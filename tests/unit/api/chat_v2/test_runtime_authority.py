"""Cluster-authoritative Chat v2 runtime overlay tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.chat_v2.schemas import RuntimeAuthority
from cognis.api.chat_v2.snapshot_coordinator import _hydrate_runtime_input
from cognis.api.chat_v2.sync import runtime_input_from_scheduler


class _RemoteOwnerScheduler:
    def active_turn_checkpoint(self, _conversation_id: str):
        return None

    def running_turn_state(self, _conversation_id: str):
        return None

    async def durable_running_turn_state(self, _conversation_id: str):
        return {
            "turn_id": "turn-remote",
            "session_id": "session-remote",
            "status": "running",
            "chat_mode": None,
            "chat_mode_source": None,
            "started_at": "2026-07-28T08:00:00+00:00",
            "updated_at": "2026-07-28T08:00:01+00:00",
        }

    async def durable_runtime_context(self, _conversation_id: str):
        return {
            "running": await self.durable_running_turn_state(_conversation_id),
            "authority": RuntimeAuthority(
                direct_request_id="request-remote",
                turn_id="turn-remote",
                fencing_token=11,
                lifecycle="active",
            ),
        }


class _RecoverableScheduler(_RemoteOwnerScheduler):
    async def durable_runtime_context(self, _conversation_id: str):
        return {
            "running": {
                "turn_id": "turn-remote",
                "session_id": "session-remote",
                "status": "waiting",
                "retry_at": "2026-07-28T10:59:00+00:00",
                "retry_reason": "rate_limit",
                "provider_id": "anthropic-demo",
                "model": "claude-fable-5",
            },
            "authority": RuntimeAuthority(
                direct_request_id="request-remote",
                turn_id="turn-remote",
                fencing_token=11,
                lifecycle="recoverable",
            ),
        }


class _ClaimedScheduler(_RemoteOwnerScheduler):
    async def durable_runtime_context(self, _conversation_id: str):
        return {
            "running": {
                "turn_id": "turn-claimed",
                "session_id": "session-remote",
                "status": "starting",
            },
            "authority": RuntimeAuthority(
                direct_request_id="request-claimed",
                turn_id="turn-claimed",
                fencing_token=12,
                lifecycle="active",
            ),
            "pending_user_message": {
                "request_id": "request-claimed",
                "turn_id": "turn-claimed",
                "content": "Summarize the work",
                "attachments": [],
                "client_message_id": "client-claimed",
                "created_at": "2026-09-16T14:39:22+00:00",
                "updated_at": "2026-09-16T14:39:25+00:00",
            },
        }


@pytest.mark.anyio
async def test_runtime_overlay_keeps_remote_durable_turn_active() -> None:
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        active_session_id="session-local",
        turn_scheduler=_RemoteOwnerScheduler(),
    )

    assert runtime.active_turn == {
        "turn_id": "turn-remote",
        "session_id": "session-remote",
        "status": "running",
        "chat_mode": None,
        "chat_mode_source": None,
        "started_at": "2026-07-28T08:00:00+00:00",
        "updated_at": "2026-07-28T08:00:01+00:00",
    }
    assert runtime.authority is not None
    assert runtime.authority.fencing_token == 11


@pytest.mark.anyio
async def test_runtime_overlay_exposes_provider_retry_deadline() -> None:
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        active_session_id="session-local",
        turn_scheduler=_RecoverableScheduler(),
    )

    assert runtime.active_turn is not None
    assert runtime.active_turn["status"] == "waiting"
    assert runtime.active_turn["retry_at"] == "2026-07-28T10:59:00+00:00"
    assert runtime.active_turn["retry_reason"] == "rate_limit"
    assert runtime.authority is not None
    assert runtime.authority.lifecycle == "recoverable"


@pytest.mark.anyio
async def test_runtime_overlay_preserves_claimed_user_message_across_refresh() -> None:
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        active_session_id="session-local",
        turn_scheduler=_ClaimedScheduler(),
    )

    assert runtime.active_turn is not None
    assert runtime.active_turn["status"] == "starting"
    assert len(runtime.volatile_items) == 1
    item = runtime.volatile_items[0]
    assert item.id == "user:client-claimed"
    assert item.kind == "message"
    assert item.sort_key == "9998:999999999999999:000000:00:000000000"
    assert item.status == "complete"
    assert item.stable is False
    assert item.role == "user"
    assert item.content == "Summarize the work"
    assert item.message_id == "client:client-claimed"
    assert item.client_message_id == "client-claimed"
    assert item.turn_id == "turn-claimed"


@pytest.mark.anyio
async def test_runtime_relay_hydration_keeps_durable_pending_user_message() -> None:
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        active_session_id="session-local",
        turn_scheduler=_ClaimedScheduler(),
    )
    relay = SimpleNamespace(
        hydrate_latest_authority=AsyncMock(
            return_value=SimpleNamespace(
                authority=runtime.authority,
                active_turn=None,
                volatile_items=[],
                volatile_items_complete=False,
            )
        )
    )
    app = SimpleNamespace(state=SimpleNamespace(chat_v2_runtime_relay=relay))

    hydrated = await _hydrate_runtime_input(app, "conversation-1", runtime)

    assert [item.id for item in hydrated.volatile_items] == ["user:client-claimed"]


@pytest.mark.anyio
@pytest.mark.parametrize("session_id", ["session-child-a", "session-child-b"])
@pytest.mark.parametrize("assigned", [True, False])
async def test_session_runtime_excludes_parent_admission(session_id: str, assigned: bool) -> None:
    scheduler = _ClaimedScheduler()
    context = await scheduler.durable_runtime_context("conversation-1")
    if not assigned:
        context["running"]["session_id"] = ""
    scheduler.durable_runtime_context = AsyncMock(return_value=context)
    cache = SimpleNamespace(get_context_usage=lambda _: {"total_tokens": 12})
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        scope_key=f"session:{session_id}",
        scope_session_ids=[session_id],
        active_session_id=session_id,
        turn_scheduler=scheduler,
        session_cache=cache,
    )
    assert runtime.active_turn is None
    assert runtime.authority is None
    assert runtime.volatile_items == []
    assert runtime.context_usage == {"total_tokens": 12}


@pytest.mark.anyio
async def test_session_runtime_preserves_owned_admission() -> None:
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        scope_session_ids=["session-remote"],
        active_session_id="session-remote",
        turn_scheduler=_ClaimedScheduler(),
    )
    assert runtime.active_turn is not None
    assert runtime.authority is not None
    assert len(runtime.volatile_items) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("session_id", ["session-remote", "session-child"])
async def test_terminal_authority_is_session_scoped(session_id: str) -> None:
    scheduler = _ClaimedScheduler()
    context = await scheduler.durable_runtime_context("conversation-1")
    context.update(
        session_id="session-remote",
        running=None,
        pending_user_message=None,
        authority=context["authority"].model_copy(update={"lifecycle": "terminal"}),
    )
    scheduler.durable_runtime_context = AsyncMock(return_value=context)
    runtime = await runtime_input_from_scheduler(
        conversation_id="conversation-1",
        scope_session_ids=[session_id],
        active_session_id=session_id,
        turn_scheduler=scheduler,
    )
    assert runtime.active_turn is None
    assert (runtime.authority is not None) == (session_id == "session-remote")
