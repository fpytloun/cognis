from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.api.routes.conversations import (
    _CHAT_LAST_OPENED_GLOBAL_STATE_KEY,
    _remember_chat_last_opened,
    _stable_assistant_timeline_id,
    _stable_thinking_timeline_id,
    project_timeline_events,
)
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_user,
    get_conversation,
    get_user_ui_state_value,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    return TestClient(create_app())


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_mark_read_emits_user_wide_unread_clear_once(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)
        send_to_user = AsyncMock()
        app.state.ws_manager = SimpleNamespace(send_to_user=send_to_user)

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="user@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Unread conversation",
                )
                last_message_at = datetime.now(UTC)
                conversation.last_message_at = last_message_at
                conversation.last_read_at = last_message_at - timedelta(minutes=1)
                await session.commit()
                return conversation.conversation_id

        conversation_id = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        response = client.post(f"/api/v1/conversations/{conversation_id}/read", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        send_to_user.assert_awaited_once()
        user_email, payload = send_to_user.await_args.args  # type: ignore[union-attr]
        assert user_email == "user@example.com"
        assert payload["type"] == "conversation_updated"
        assert payload["conversation_id"] == conversation_id
        assert payload["has_unread"] is False
        assert isinstance(payload["last_read_at"], str)

        async def _last_read_at() -> datetime | None:
            async with app.state.session_factory() as session:
                conversation = await get_conversation(session, conversation_id)
                return conversation.last_read_at if conversation else None

        stored_last_read_at = asyncio.run(_last_read_at())
        assert stored_last_read_at is not None
        assert (
            datetime.fromisoformat(payload["last_read_at"]).replace(tzinfo=None)
            == stored_last_read_at
        )

        response = client.post(f"/api/v1/conversations/{conversation_id}/read", headers=headers)

        assert response.status_code == 200
        send_to_user.assert_awaited_once()

        assert send_to_user.await_args_list == [call("user@example.com", payload)]


def test_slash_command_suggestions_route_returns_dispatcher_items(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-chat",
                    owner_email="user@example.com",
                    name="Agent",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    context_type="web",
                    title="Suggestions conversation",
                )
                await create_session(
                    session,
                    session_id="sess-suggestions",
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    status="active",
                )
                conversation.active_session_id = "sess-suggestions"
                await session.commit()
                return conversation.conversation_id

        class _Dispatcher:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def suggest(self, command_input: str, **kwargs: object) -> list[object]:
                self.calls.append({"command_input": command_input, **kwargs})
                return [
                    SimpleNamespace(
                        kind="parameter",
                        command="/skill",
                        value="cognis-coding",
                        label="Cognis Coding",
                        description="Coding guidance",
                        insert_text="/skill cognis-coding",
                        suffix="none",
                        badges=["loaded"],
                    )
                ]

        dispatcher = _Dispatcher()
        app.state.command_dispatcher = dispatcher
        conversation_id = asyncio.run(_seed())

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/slash-command-suggestions",
            params={"input": "/skill cog", "limit": 5},
            headers=_auth_headers(app, email="user@example.com"),
        )

        assert response.status_code == 200
        assert response.json() == {
            "items": [
                {
                    "kind": "parameter",
                    "command": "/skill",
                    "value": "cognis-coding",
                    "label": "Cognis Coding",
                    "description": "Coding guidance",
                    "insert_text": "/skill cognis-coding",
                    "suffix": "none",
                    "badges": ["loaded"],
                }
            ]
        }
        assert dispatcher.calls[0]["command_input"] == "/skill cog"
        assert dispatcher.calls[0]["limit"] == 5


# ---------------------------------------------------------------------------
# Global last-opened key tests (Issue 3 — PWA conversation-first restore)
# ---------------------------------------------------------------------------


def test_remember_chat_last_opened_writes_global_key(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """_remember_chat_last_opened must write the agent-agnostic global key."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _run() -> dict[str, Any] | None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("pw"),
                    role="user",
                )
                await _remember_chat_last_opened(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-a",
                    context_type="web",
                    agent_profile_id=None,
                    conversation_id="conv-123",
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                await session.commit()
                return await get_user_ui_state_value(
                    session, "user@example.com", _CHAT_LAST_OPENED_GLOBAL_STATE_KEY
                )

        state = asyncio.run(_run())
        assert state is not None
        assert state["conversation_id"] == "conv-123"
        assert state["agent_id"] == "agent-a"
        assert state["context_type"] == "web"


def test_open_conversation_global_key_fallback(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    """open_conversation must restore the last-opened conversation via the global key
    even when the request's agent_id differs from the conversation's agent."""
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = cast(Any, client.app)

        async def _seed() -> tuple[str, str]:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="User",
                    password_hash=app.state.password_hasher.hash("pw"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="agent-a",
                    owner_email="user@example.com",
                    name="Agent A",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="agent-b",
                    owner_email="user@example.com",
                    name="Agent B",
                    status="active",
                )
                # Conversation belongs to agent-b
                conv = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-b",
                    context_type="web",
                    title="Last opened",
                )
                # Write global key pointing at agent-b's conversation
                await _remember_chat_last_opened(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-b",
                    context_type="web",
                    agent_profile_id=None,
                    conversation_id=conv.conversation_id,
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
                await session.commit()
                return conv.conversation_id, "agent-a"

        conv_id, requesting_agent = asyncio.run(_seed())
        headers = _auth_headers(app, email="user@example.com")

        # Request with agent-a — no agent-a conversations exist, but the global
        # key points at agent-b's conversation. The endpoint should return it.
        response = client.post(
            "/api/v1/conversations/open",
            json={"agent_id": requesting_agent, "context_type": "web"},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["conversation_id"] == conv_id


# ---------------------------------------------------------------------------
# Compaction projection tests (Issue 4B — rotation/context_seed markers)
# ---------------------------------------------------------------------------


def test_project_timeline_events_emits_compaction_card_for_rotation_marker() -> None:
    """project_timeline_events must emit a compaction card for rotation/context_seed
    markers (previously skipped, causing the live compaction box to be dropped on
    history refresh)."""
    from cognis.api.models import MessageEventResponse

    events = [
        MessageEventResponse(
            type="compaction_summary",
            seq=1,
            timestamp="2026-01-01T00:00:00+00:00",
            data={
                "summary": "Compacted 5 turns.",
                "method": "rotation",
                "marker_role": "context_seed",
                "session_id": "sess-new",
                "source_session_id": "sess-old",
                "turns_compacted": 5,
                "timeline_visible": True,
            },
        )
    ]
    items = project_timeline_events(events)
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "compaction"
    assert item["status"] == "compacted"
    assert item["id"] == "compaction:sess-old:sess-new"
    assert item["turnsCompacted"] == 5


def test_project_timeline_events_skips_compaction_card_when_timeline_visible_false() -> None:
    """project_timeline_events must skip compaction_summary with timeline_visible=False."""
    from cognis.api.models import MessageEventResponse

    events = [
        MessageEventResponse(
            type="compaction_summary",
            seq=1,
            timestamp="2026-01-01T00:00:00+00:00",
            data={
                "summary": "Internal.",
                "method": "rotation",
                "marker_role": "context_seed",
                "session_id": "sess-new",
                "source_session_id": "sess-old",
                "timeline_visible": False,
            },
        )
    ]
    items = project_timeline_events(events)
    assert len(items) == 0


# ---------------------------------------------------------------------------
# Layer A: Phase inference unification + identity tests
# ---------------------------------------------------------------------------


class TestAssistantTimelineIdentity:
    """Live stream, completion patch, and history replay must produce the same
    item id for the same logical assistant message."""

    def test_stable_id_matches_for_phase_zero(self) -> None:
        message_id = "turn_abc"
        phase = 0
        # All three paths use _stable_assistant_timeline_id
        expected = f"message:{message_id}:phase:{phase}"
        assert _stable_assistant_timeline_id(message_id, phase, message_id) == expected

    def test_stable_id_matches_for_multi_phase(self) -> None:
        message_id = "turn_abc"
        for phase in range(4):
            expected = f"message:{message_id}:phase:{phase}"
            assert _stable_assistant_timeline_id(message_id, phase, message_id) == expected

    def test_stable_id_fallback_when_no_message_id(self) -> None:
        # When message_id is empty the fallback eid is used
        result = _stable_assistant_timeline_id("", 0, "fallback_eid")
        assert result == "event:fallback_eid:assistant"

    def test_project_timeline_events_uses_explicit_phase(self) -> None:
        """History projector must use the explicit assistant_phase_index from
        event data, not infer it, when the field is present."""
        from cognis.api.models import MessageEventResponse

        events = [
            MessageEventResponse(
                type="user_message",
                seq=1,
                timestamp="2026-01-01T00:00:00+00:00",
                data={"content": "Hello", "turn_id": "turn_1"},
            ),
            MessageEventResponse(
                type="assistant_message",
                seq=2,
                timestamp="2026-01-01T00:00:01+00:00",
                data={
                    "content": "Hi there",
                    "turn_id": "turn_1",
                    "message_id": "turn_1",
                    "assistant_phase_index": 0,
                    "turn_cycle_index": 0,
                },
            ),
            MessageEventResponse(
                type="tool_call",
                seq=3,
                timestamp="2026-01-01T00:00:02+00:00",
                data={
                    "call_id": "call_1",
                    "tool_name": "read",
                    "status": "completed",
                    "turn_id": "turn_1",
                    "assistant_phase_index": 0,
                    "turn_cycle_index": 0,
                },
            ),
            MessageEventResponse(
                type="assistant_message",
                seq=4,
                timestamp="2026-01-01T00:00:03+00:00",
                data={
                    "content": "Done.",
                    "turn_id": "turn_1",
                    "message_id": "turn_1",
                    "assistant_phase_index": 1,
                },
            ),
        ]
        items = project_timeline_events(events)
        assistant_items = [
            i for i in items if i.get("kind") == "message" and i.get("role") == "assistant"
        ]
        assert len(assistant_items) == 2
        # Phase 0 message
        assert assistant_items[0]["id"] == "message:turn_1:phase:0"
        assert assistant_items[0]["assistantPhaseIndex"] == 0
        assert assistant_items[0]["turnCycleIndex"] == 0
        tool_items = [i for i in items if i.get("kind") == "tool_call"]
        assert tool_items[0]["turnCycleIndex"] == 0
        # Phase 1 message
        assert assistant_items[1]["id"] == "message:turn_1:phase:1"
        assert assistant_items[1]["assistantPhaseIndex"] == 1

    def test_project_timeline_events_preserves_cycle_when_merging_assistant_updates(self) -> None:
        """A later assistant update for the same message may carry metadata the
        first partial did not; merging must not leave stale missing cycle data."""
        from cognis.api.models import MessageEventResponse

        events = [
            MessageEventResponse(
                type="assistant_message",
                seq=1,
                timestamp="2026-01-01T00:00:01+00:00",
                data={
                    "content": "The patch failed;",
                    "turn_id": "turn_1",
                    "message_id": "turn_1",
                    "assistant_phase_index": 0,
                    "partial": True,
                },
            ),
            MessageEventResponse(
                type="assistant_message",
                seq=2,
                timestamp="2026-01-01T00:00:02+00:00",
                data={
                    "content": " I will read the exact files.",
                    "turn_id": "turn_1",
                    "message_id": "turn_1",
                    "assistant_phase_index": 0,
                    "turn_cycle_index": 2,
                    "partial": False,
                },
            ),
            MessageEventResponse(
                type="tool_call",
                seq=3,
                timestamp="2026-01-01T00:00:03+00:00",
                data={
                    "call_id": "call_1",
                    "tool_name": "read",
                    "status": "completed",
                    "turn_id": "turn_1",
                    "assistant_phase_index": 1,
                    "turn_cycle_index": 2,
                },
            ),
        ]

        items = project_timeline_events(events)

        assistant_item = next(
            i for i in items if i.get("kind") == "message" and i.get("role") == "assistant"
        )
        tool_item = next(i for i in items if i.get("kind") == "tool_call")
        assert assistant_item["turnCycleIndex"] == 2
        assert tool_item["turnCycleIndex"] == 2


class TestThinkingTimelineIdentity:
    """Runtime thinking snapshot and history replay must produce the same item id
    for the same logical thinking segment so the id-keyed client store can merge
    them without orphaning or duplicating."""

    def test_stable_thinking_id_format(self) -> None:
        assert _stable_thinking_timeline_id("msg_1", 0, "blk_1") == "thinking:msg_1:phase:0:blk_1"
        assert _stable_thinking_timeline_id("msg_1", 1, "blk_2") == "thinking:msg_1:phase:1:blk_2"
        # phase None fallback matches the canonical projector (no :phase: component).
        assert _stable_thinking_timeline_id("msg_1", None, "blk_3") == "thinking:msg_1:blk_3"
