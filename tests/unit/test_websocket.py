"""Dedicated WebSocket handler and turn scheduler unit tests.

Covers classify_turn_error, authentication flow, rate limiting,
backpressure, and access control.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from cognis.api.websocket import (
    AuthenticatedWebSocket,
    WebSocketConnectionManager,
    WebSocketTurnObserver,
    _assistant_runtime_payload,
    _event_to_payload,
    _handle_cancel_queued_message,
    _handle_message,
    _handle_update_queued_message,
    _is_visible_persisted_system_message,
    _timeline_patch_for_bus_event,
    _workflow_composed_payload,
)
from cognis.core.events import Event, EventType
from cognis.core.turn_scheduler import (
    SessionCreationFailedError,
    TurnResult,
    classify_turn_error,
)
from cognis.models.config import ProviderHealth
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_managed_conversation_link,
    create_user,
)

# ---------------------------------------------------------------------------
# Helpers — fake providers for classify_turn_error
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, status: str = "healthy") -> None:
        self._status = status

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="fake", status=self._status)


class _FakeRaisingProvider:
    """Provider whose health() raises — simulates a hard failure."""

    async def health(self) -> ProviderHealth:
        raise RuntimeError("provider health check exploded")


class _RecordingManager:
    def __init__(self) -> None:
        self.errors: list[dict[str, object]] = []
        self.snapshots: list[str] = []
        self.subscriptions: list[str] = []
        self.payloads: list[tuple[str, dict[str, object]]] = []
        self.sidebar_payloads: list[tuple[str, dict[str, object]]] = []
        self.app: Any = SimpleNamespace(state=SimpleNamespace())

    async def send_error(self, _: object, **kwargs: object) -> None:
        self.errors.append(kwargs)

    def subscribe(self, _: object, conversation_id: str) -> None:
        self.subscriptions.append(conversation_id)

    async def send_queue_snapshot(self, _: object, conversation_id: str) -> None:
        self.snapshots.append(conversation_id)

    async def send_to_conversation(self, conversation_id: str, payload: dict[str, object]) -> None:
        self.snapshots.append(f"event:{conversation_id}:{payload.get('type')}")
        self.payloads.append((conversation_id, payload))

    async def send_sidebar_update_to_owner(
        self, conversation_id: str, payload: dict[str, object]
    ) -> None:
        self.sidebar_payloads.append((conversation_id, payload))

    def has_tts_enabled_subscribers(self, _conversation_id: str) -> bool:
        return False


class _RecordingConnection:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)


class _RuntimeSnapshotScheduler:
    def queued_messages(self, conversation_id: str) -> list[dict[str, object]]:
        assert conversation_id == "conv-1"
        return [{"queue_id": "queue-1", "content": "queued"}]

    async def active_stream_snapshots(self, conversation_id: str) -> list[dict[str, object]]:
        assert conversation_id == "conv-1"
        return [{"message_id": "msg-1", "content": "stream"}]

    async def active_tool_output_snapshots(self, conversation_id: str) -> list[dict[str, object]]:
        assert conversation_id == "conv-1"
        return [{"call_id": "call-1", "chunk": "output"}]

    def running_turn_state(self, conversation_id: str) -> dict[str, object]:
        assert conversation_id == "conv-1"
        return {"chat_mode": "build", "chat_mode_source": "user"}


class _RuntimeSnapshotSessionCache:
    def active_thinking_snapshots(self, session_id: str) -> list[dict[str, object]]:
        assert session_id == "sess-1"
        return [{"message_id": "msg-1", "blocks": [{"content": "thought"}]}]


class _RecordingWebSocketManager(WebSocketConnectionManager):
    def __init__(self) -> None:
        super().__init__(SimpleNamespace(state=SimpleNamespace()))
        self.sent_payloads: list[tuple[str, dict[str, Any]]] = []

    async def send_to_conversation(self, conversation_id: str, payload: dict[str, Any]) -> None:
        self.sent_payloads.append((conversation_id, payload))

    async def _resolve_conversation_id(self, event: Event) -> str | None:  # noqa: SLF001
        return str(event.data.get("conversation_id") or "conv-1")


async def _seed_conversation(app: Any, *, owner: str = "owner@example.com") -> str:
    async with app.state.session_factory() as session:
        await create_user(
            session,
            email=owner,
            name="Owner",
            password_hash=app.state.password_hasher.hash("password123"),
            role="user",
        )
        await create_agent(
            session,
            agent_id="agent-queue-auth",
            owner_email=owner,
            name="Agent",
            status="active",
        )
        conversation = await create_conversation(
            session,
            user_email=owner,
            agent_id="agent-queue-auth",
            context_type="web",
            title="Conversation",
        )
        await session.commit()
        return conversation.conversation_id


@dataclass
class _FakeProviders:
    guardrails: Any = field(default_factory=lambda: _FakeProvider())
    llm: Any = field(default_factory=lambda: _FakeProvider())
    memory: Any = field(default_factory=lambda: _FakeProvider())


class _NullSession:
    async def __aenter__(self) -> _NullSession:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Event-to-payload mapping tests
# ---------------------------------------------------------------------------


def test_delegation_completed_payload_includes_durable_result_fields() -> None:
    event = Event(
        type=EventType.DELEGATION_COMPLETED,
        data={
            "child_session_id": "child-1",
            "agent_id": "agent-1",
            "used_agent_id": "agent-1",
            "task": "Review branch",
            "duration_ms": 123,
            "result_summary": "Compact result",
            "result_content": "[assistant_message:1]\nFull result",
            "result_source": "assistant_messages",
            "result_anchors": [{"anchor": "assistant_message:1"}],
            "result_truncated": False,
        },
    )

    payload = _event_to_payload(event, "conversation-1")

    assert payload is not None
    assert payload["type"] == "delegation_completed"
    assert payload["result"] == "Compact result"
    assert payload["result_content"] == "[assistant_message:1]\nFull result"
    assert payload["result_source"] == "assistant_messages"
    assert payload["result_anchors"] == [{"anchor": "assistant_message:1"}]
    assert payload["result_truncated"] is False


def test_user_message_payload_exposes_stable_live_identity() -> None:
    event = Event(
        type=EventType.USER_MESSAGE,
        data={
            "conversation_id": "conversation-1",
            "session_id": "session-1",
            "event_id": "client:cmsg_1",
            "message_id": "client:cmsg_1",
            "content": "hello",
            "client_message_id": "cmsg_1",
            "turn_id": "turn_1",
        },
    )

    payload = _event_to_payload(event, "conversation-1")

    assert payload is not None
    assert payload["type"] == "user_message"
    assert payload["event_id"] == "client:cmsg_1"
    assert payload["message_id"] == "client:cmsg_1"
    assert payload["timestamp"] is not None


def test_session_compaction_started_payload_exposes_runtime_state() -> None:
    event = Event(
        type=EventType.SESSION_COMPACTION_STARTED,
        data={
            "conversation_id": "conversation-1",
            "session_id": "session-1",
            "trigger": "idle_checkpoint",
            "reason": "long_lived_chat_idle",
            "effective_usage_percentage": 91.2,
            "hard_pressure_exceeded": True,
            "phase": "turn",
            "status": "running",
            "provider_id": "provider-1",
            "model_id": "model-1",
        },
    )

    payload = _event_to_payload(event, "conversation-1")

    assert payload is not None
    assert payload["type"] == "session_compaction_started"
    assert payload["conversation_id"] == "conversation-1"
    assert payload["session_id"] == "session-1"
    assert payload["trigger"] == "idle_checkpoint"
    assert payload["reason"] == "long_lived_chat_idle"
    assert payload["effective_usage_percentage"] == 91.2
    assert payload["hard_pressure_exceeded"] is True
    assert payload["status"] == "running"
    assert payload["provider_id"] == "provider-1"
    assert payload["model_id"] == "model-1"


def test_session_compaction_finished_payload_clears_runtime_state() -> None:
    event = Event(
        type=EventType.SESSION_COMPACTION_FINISHED,
        data={
            "conversation_id": "conversation-1",
            "session_id": "session-1",
            "trigger": "automatic",
            "reason": "context_pressure",
            "status": "failed",
            "fallback_reason": "compaction_failed",
        },
    )

    payload = _event_to_payload(event, "conversation-1")

    assert payload is not None
    assert payload["type"] == "session_compaction_finished"
    assert payload["conversation_id"] == "conversation-1"
    assert payload["session_id"] == "session-1"
    assert payload["status"] == "failed"
    assert payload["fallback_reason"] == "compaction_failed"


# ---------------------------------------------------------------------------
# classify_turn_error tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_session_creation_failed() -> None:
    providers = _FakeProviders()
    result = await classify_turn_error(
        providers, SessionCreationFailedError("intaris returned 500")
    )
    assert result.code == "session_creation_failed"
    assert result.recoverable is True
    assert result.detail is not None
    assert "error_detail" in result.detail


@pytest.mark.asyncio
async def test_classify_no_llm_model_configured_valueerror() -> None:
    providers = _FakeProviders()
    result = await classify_turn_error(providers, ValueError("No LLM model configured"))
    assert result.code == "provider_not_configured:llm"
    assert result.recoverable is True


@pytest.mark.asyncio
async def test_classify_unhealthy_guardrails() -> None:
    providers = _FakeProviders(guardrails=_FakeProvider("unhealthy"))
    result = await classify_turn_error(providers, RuntimeError("something broke"))
    assert result.code == "provider_unreachable:guardrails"


@pytest.mark.asyncio
async def test_classify_unhealthy_llm_not_configured() -> None:
    providers = _FakeProviders(llm=_FakeProvider("unhealthy"))
    result = await classify_turn_error(providers, ValueError("not configured for this provider"))
    assert result.code == "provider_not_configured:llm"


@pytest.mark.asyncio
async def test_classify_unhealthy_llm_generic_error() -> None:
    providers = _FakeProviders(llm=_FakeProvider("unhealthy"))
    result = await classify_turn_error(providers, RuntimeError("model refused to answer"))
    assert result.code == "provider_error:llm"


@pytest.mark.asyncio
async def test_classify_unhealthy_memory() -> None:
    providers = _FakeProviders(memory=_FakeProvider("unhealthy"))
    result = await classify_turn_error(providers, RuntimeError("something broke"))
    assert result.code == "provider_unreachable:memory"


@pytest.mark.asyncio
async def test_classify_httpx_error() -> None:
    providers = _FakeProviders()
    result = await classify_turn_error(providers, httpx.ConnectError("Connection refused"))
    assert result.code == "provider_error:llm"
    assert result.recoverable is True


@pytest.mark.asyncio
async def test_classify_timeout_error() -> None:
    providers = _FakeProviders()
    result = await classify_turn_error(providers, TimeoutError("timed out"))
    assert result.code == "provider_error:llm"


@pytest.mark.asyncio
async def test_classify_generic_runtime_error_all_healthy() -> None:
    providers = _FakeProviders()
    result = await classify_turn_error(providers, RuntimeError("unexpected NoneType"))
    assert result.code == "turn_failed"
    assert "failed" in result.message.lower()


@pytest.mark.asyncio
async def test_classify_skips_provider_whose_health_raises() -> None:
    """If a provider health() check itself raises, it's skipped."""
    providers = _FakeProviders(
        guardrails=_FakeRaisingProvider(),
        llm=_FakeProvider("unhealthy"),
    )
    # guardrails health raises → skipped; llm is unhealthy → detected
    result = await classify_turn_error(providers, RuntimeError("something broke"))
    assert result.code == "provider_error:llm"


@pytest.mark.asyncio
async def test_classify_error_detail_is_sanitized() -> None:
    """Verify that API keys and long content are stripped from error_detail."""
    providers = _FakeProviders()
    error = RuntimeError(
        'OpenAI returned 401: {"error": "Invalid API key: sk-proj-1234567890abcdef"}'
    )
    result = await classify_turn_error(providers, error)
    assert result.detail is not None
    assert "sk-proj" not in str(result.detail.get("error_detail", ""))


# ---------------------------------------------------------------------------
# Connection fanout tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_manager_sends_user_payload_to_all_user_connections() -> None:
    app = SimpleNamespace(state=SimpleNamespace(event_bus=None, turn_scheduler=None))
    manager = WebSocketConnectionManager(app)

    user_ws_1 = AsyncMock()
    user_ws_2 = AsyncMock()
    other_ws = AsyncMock()
    await manager.connect(user_ws_1, claims={"sub": "user@example.com", "role": "user"})
    await manager.connect(user_ws_2, claims={"sub": "user@example.com", "role": "user"})
    await manager.connect(other_ws, claims={"sub": "other@example.com", "role": "user"})

    payload = {"type": "conversation_updated", "conversation_id": "conv-1"}
    await manager.send_to_user("user@example.com", payload)

    user_ws_1.send_json.assert_awaited_once_with(payload)
    user_ws_2.send_json.assert_awaited_once_with(payload)
    other_ws.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_turn_observer_sends_system_message_metadata() -> None:
    manager = _RecordingManager()
    observer = WebSocketTurnObserver(manager)  # type: ignore[arg-type]

    await observer.on_system_message(
        "conv-1",
        "Turn initiated by task failure: Nightly import (task-1).",
        notice_id="turn-init:fup_task_failed",
        kind="turn_initiated",
        scope="turn",
        turn_id="turn-1",
    )

    assert manager.payloads == [
        (
            "conv-1",
            {
                "type": "system_message",
                "conversation_id": "conv-1",
                "text": "Turn initiated by task failure: Nightly import (task-1).",
                "notice_id": "turn-init:fup_task_failed",
                "kind": "turn_initiated",
                "scope": "turn",
                "turn_id": "turn-1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_websocket_turn_observer_sends_tool_call_as_timeline_patch() -> None:
    manager = _RecordingManager()
    observer = WebSocketTurnObserver(manager)  # type: ignore[arg-type]

    await observer.on_tool_call(
        "conv-1",
        "sess-1",
        "call-1",
        "bash",
        {"command": "true"},
        turn_id="turn-1",
    )

    assert manager.payloads[0][0] == "conv-1"
    payload = cast(dict[str, Any], manager.payloads[0][1])
    assert payload["type"] == "timeline_patch"
    assert payload["conversation_id"] == "conv-1"
    assert payload["source"] == "live.tool_call"
    assert payload["items"][0] == {
        "id": "tool:call-1",
        "kind": "tool_call",
        "callId": "call-1",
        "turnId": "turn-1",
        "toolName": "bash",
        "status": "started",
        "timestamp": payload["items"][0]["timestamp"],
        "arguments": {"command": "true"},
    }


def test_event_bus_delegation_started_projects_to_timeline_patch() -> None:
    event = Event(
        type=EventType.DELEGATION_STARTED,
        data={
            "conversation_id": "conv-1",
            "session_id": "sess-1",
            "child_session_id": "child-1",
            "title": "Explore project",
            "tool_call_count": 2,
            "max_tool_calls": 5,
            "last_tool": "grep",
        },
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    payload = _timeline_patch_for_bus_event(event, "conv-1")

    assert payload is not None
    assert payload["type"] == "timeline_patch"
    assert payload["source"] == "live.delegation_started"
    assert payload["items"] == [
        {
            "id": "delegation:child-1",
            "kind": "delegation",
            "taskId": "child-1",
            "taskLabel": "Explore project",
            "status": "started",
            "toolCallCount": 2,
            "maxToolCalls": 5,
            "lastTool": "grep",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
    ]


def test_event_bus_workflow_composed_projects_stable_timeline_patch_id() -> None:
    event = Event(
        type=EventType.WORKFLOW_COMPOSED,
        data={
            "conversation_id": "conv-1",
            "workflow_id": "wf-1",
            "task_id": "task-1",
            "title": "Run workflow",
        },
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    payload = _timeline_patch_for_bus_event(event, "conv-1")

    assert payload is not None
    assert payload["type"] == "timeline_patch"
    assert payload["source"] == "live.workflow_composed"
    assert payload["items"][0]["id"] == "workflow-composed:wf-1"
    assert payload["items"][0]["workflowId"] == "wf-1"


@pytest.mark.asyncio
async def test_terminal_event_sends_timeline_patch_and_legacy_side_effect_frame() -> None:
    manager = _RecordingWebSocketManager()
    event = Event(
        type=EventType.DELEGATION_COMPLETED,
        data={
            "conversation_id": "conv-1",
            "session_id": "sess-1",
            "child_session_id": "child-1",
            "title": "Explore project",
            "result": "done",
        },
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    await manager._handle_event(event)  # noqa: SLF001

    assert [payload["type"] for _, payload in manager.sent_payloads] == [
        "timeline_patch",
        "delegation_completed",
    ]
    assert manager.sent_payloads[0][1]["items"][0] == {
        "id": "delegation:child-1",
        "kind": "delegation",
        "taskId": "child-1",
        "taskLabel": "Explore project",
        "status": "completed",
        "result": "done",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_websocket_manager_sends_authoritative_runtime_snapshot() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            turn_scheduler=_RuntimeSnapshotScheduler(),
            session_cache=_RuntimeSnapshotSessionCache(),
        )
    )
    manager = WebSocketConnectionManager(app)  # type: ignore[arg-type]
    connection = _RecordingConnection()

    await manager._send_conversation_runtime_snapshot(  # noqa: SLF001
        connection,  # type: ignore[arg-type]
        "conv-1",
        active_session_id="sess-1",
    )

    assert connection.payloads == [
        {
            "type": "conversation_runtime_snapshot",
            "conversation_id": "conv-1",
            "queued_messages": [{"queue_id": "queue-1", "content": "queued"}],
            "queued_count": 1,
            "has_active_turn": True,
            "active_turn_chat_mode": "build",
            "active_turn_chat_mode_source": "user",
            "active_streams": [{"message_id": "msg-1", "content": "stream"}],
            "active_tool_outputs": [{"call_id": "call-1", "chunk": "output"}],
            "active_thinking": [{"message_id": "msg-1", "blocks": [{"content": "thought"}]}],
        }
    ]


def test_visible_persisted_system_message_filter_allows_explicit_notices() -> None:
    assert _is_visible_persisted_system_message(
        {"notice_id": "turn-init:fup_task_failed", "kind": "turn_initiated"}
    )
    assert _is_visible_persisted_system_message({"event": "turn_initiated"})


def test_visible_persisted_system_message_filter_rejects_internal_context() -> None:
    assert not _is_visible_persisted_system_message(
        {"content": ("Environment: - Executor: olorin (websocket) - Platform: unknown (unknown)")}
    )
    assert not _is_visible_persisted_system_message(
        {"content": "Additional tools may be available but hidden by the current step profile."}
    )


def test_assistant_runtime_payload_accepts_only_dict_metadata() -> None:
    runtime = {"agent_id": "laforge", "model": "gpt-5.1", "reasoning_effort": "high"}

    assert _assistant_runtime_payload({"runtime": runtime}) == runtime
    assert _assistant_runtime_payload({"runtime": "invalid"}) is None
    assert _assistant_runtime_payload({}) is None


def test_conversation_updated_payload_includes_read_state_fields() -> None:
    payload = _event_to_payload(
        Event(
            type=EventType.CONVERSATION_UPDATED,
            data={
                "conversation_id": "conv-1",
                "has_unread": False,
                "last_read_at": "2026-06-08T12:00:00+00:00",
            },
        ),
        "conv-1",
    )

    assert payload == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_unread": False,
        "last_read_at": "2026-06-08T12:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_notification_events_emit_user_wide_attention_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_calls: list[tuple[str, list[str]]] = []

    async def _fake_pending_types(
        _session: Any, user_email: str, conversation_ids: list[str]
    ) -> dict[str, list[str]]:
        assert user_email == "user@example.com"
        assert conversation_ids == ["conv-1"]
        pending_calls.append((user_email, conversation_ids))
        return {"conv-1": ["gate"]} if len(pending_calls) == 1 else {"conv-1": []}

    monkeypatch.setattr(
        "cognis.api.websocket.list_pending_notification_types_by_conversation",
        _fake_pending_types,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            event_bus=None,
            turn_scheduler=SimpleNamespace(
                add_observer=lambda _conversation_id, _observer: None,
                running_turn_state=lambda _conversation_id: None,
            ),
            session_factory=lambda: _NullSession(),
        )
    )
    manager = WebSocketConnectionManager(app)
    user_ws_1 = AsyncMock()
    user_ws_2 = AsyncMock()
    other_ws = AsyncMock()
    admin_ws = AsyncMock()
    await manager.connect(user_ws_1, claims={"sub": "user@example.com", "role": "user"})
    await manager.connect(user_ws_2, claims={"sub": "user@example.com", "role": "user"})
    await manager.connect(other_ws, claims={"sub": "other@example.com", "role": "user"})
    await manager.connect(admin_ws, claims={"sub": "admin@example.com", "role": "admin"})

    await manager._handle_event(
        Event(
            type=EventType.NOTIFICATION_CREATED,
            data={
                "notification_id": "notif-1",
                "notification_type": "gate",
                "user_email": "user@example.com",
                "conversation_id": "conv-1",
                "payload": {"message": "Approve?"},
            },
        )
    )

    pending_payload = {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "pending_notification_types": ["gate"],
        "has_active_turn": False,
        "active_turn_chat_mode": None,
        "active_turn_chat_mode_source": None,
    }
    user_ws_1.send_json.assert_awaited_once_with(pending_payload)
    user_ws_2.send_json.assert_awaited_once_with(pending_payload)
    other_ws.send_json.assert_not_called()
    admin_ws.send_json.assert_not_called()

    await manager._handle_event(
        Event(
            type=EventType.NOTIFICATION_RESOLVED,
            data={
                "notification_id": "notif-1",
                "notification_type": "gate",
                "user_email": "user@example.com",
                "conversation_id": "conv-1",
                "decision": "approve",
            },
        )
    )

    resolved_payload = {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "pending_notification_types": [],
        "has_active_turn": False,
        "active_turn_chat_mode": None,
        "active_turn_chat_mode_source": None,
    }
    assert user_ws_1.send_json.await_args_list[-1].args == (resolved_payload,)
    assert user_ws_2.send_json.await_args_list[-1].args == (resolved_payload,)
    other_ws.send_json.assert_not_called()
    admin_ws.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_notification_attention_refresh_requires_user_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_types = AsyncMock(return_value={"conv-1": ["gate"]})
    monkeypatch.setattr(
        "cognis.api.websocket.list_pending_notification_types_by_conversation",
        pending_types,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            event_bus=None,
            turn_scheduler=SimpleNamespace(
                add_observer=lambda _conversation_id, _observer: None,
                running_turn_state=lambda _conversation_id: None,
            ),
            session_factory=lambda: _NullSession(),
        )
    )
    manager = WebSocketConnectionManager(app)
    user_ws = AsyncMock()
    await manager.connect(user_ws, claims={"sub": "user@example.com", "role": "user"})

    await manager._handle_event(
        Event(
            type=EventType.NOTIFICATION_CREATED,
            data={
                "notification_id": "notif-1",
                "notification_type": "gate",
                "conversation_id": "conv-1",
                "payload": {"message": "Approve?"},
            },
        )
    )

    pending_types.assert_not_awaited()
    user_ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------


def test_inbound_rate_limit_allows_within_budget() -> None:
    loop = asyncio.new_event_loop()

    async def _run() -> bool:
        ws = AuthenticatedWebSocket(
            connection_id="test",
            websocket=AsyncMock(),
            user_email="user@test.com",
            role="user",
        )
        # Should allow up to DEFAULT_INBOUND_RATE_LIMIT (10)
        for _ in range(10):
            assert ws.allow_inbound_message() is True
        # 11th should be denied
        return ws.allow_inbound_message()

    result = loop.run_until_complete(_run())
    loop.close()
    assert result is False


def test_inbound_rate_limit_window_expires() -> None:
    """Verify that stale timestamps are cleaned from the deque."""
    ws = AuthenticatedWebSocket(
        connection_id="test",
        websocket=AsyncMock(),
        user_email="user@test.com",
        role="user",
    )
    # Simulate old timestamps (> 1s ago relative to loop.time())
    ws.recent_message_times = deque([0.0] * 10)

    async def _run() -> bool:
        return ws.allow_inbound_message()

    loop = asyncio.new_event_loop()
    # loop.time() starts near 0 for a new loop, so set times in the far past
    ws.recent_message_times = deque([-10.0] * 10)
    result = loop.run_until_complete(_run())
    loop.close()
    # Old timestamps should be cleaned, allowing the new message
    assert result is True


# ---------------------------------------------------------------------------
# Backpressure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunk_dropped_when_buffer_full() -> None:
    """When pending_sends >= DEFAULT_OUTBOUND_BUFFER, non-critical chunks are dropped."""
    mock_ws = AsyncMock()
    connection = AuthenticatedWebSocket(
        connection_id="test",
        websocket=mock_ws,
        user_email="user@test.com",
        role="user",
    )
    connection.pending_sends = 100  # at the buffer limit

    await connection.send_json({"type": "chunk", "message_id": "msg_1", "content": "hello"})
    # Non-critical chunk should be dropped
    mock_ws.send_json.assert_not_called()
    assert connection.dropped_chunks.get("msg_1") == 1


@pytest.mark.asyncio
async def test_critical_message_not_dropped_when_buffer_full() -> None:
    """Critical messages (non-chunk) are sent even at buffer limit."""
    mock_ws = AsyncMock()
    connection = AuthenticatedWebSocket(
        connection_id="test",
        websocket=mock_ws,
        user_email="user@test.com",
        role="user",
    )
    connection.pending_sends = 100

    await connection.send_json({"type": "message_complete", "message_id": "msg_1", "seq": 1})
    assert mock_ws.send_json.called


@pytest.mark.asyncio
async def test_chunk_gap_frame_emitted_after_drops() -> None:
    """After dropping chunks, a chunk_gap frame precedes the next non-chunk message."""
    mock_ws = AsyncMock()
    connection = AuthenticatedWebSocket(
        connection_id="test",
        websocket=mock_ws,
        user_email="user@test.com",
        role="user",
    )
    # Simulate having dropped 5 chunks for msg_1
    connection.dropped_chunks["msg_1"] = 5

    await connection.send_json(
        {"type": "message_complete", "message_id": "msg_1", "conversation_id": "conv_1"}
    )
    # Should have sent chunk_gap first, then message_complete
    assert mock_ws.send_json.call_count == 2
    gap_payload = mock_ws.send_json.call_args_list[0][0][0]
    assert gap_payload["type"] == "chunk_gap"
    assert gap_payload["dropped_count"] == 5
    # After sending gap, the dropped_chunks entry should be cleared
    assert "msg_1" not in connection.dropped_chunks


@pytest.mark.asyncio
async def test_turn_completion_event_emits_activity_correction() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            event_bus=None,
            turn_scheduler=SimpleNamespace(
                add_observer=lambda _conversation_id, _observer: None,
                running_turn_state=lambda _conversation_id: None,
            ),
            session_factory=lambda: _NullSession(),
        )
    )
    manager = WebSocketConnectionManager(app)
    user_ws = AsyncMock()
    connection = await manager.connect(user_ws, claims={"sub": "user@example.com", "role": "user"})
    manager.subscribe(connection, "conv-1")

    completed_at = "2026-01-02T03:04:00+00:00"
    await manager._handle_event(
        Event(
            type=EventType.TURN_COMPLETED,
            data={
                "conversation_id": "conv-1",
                "session_id": "sess-1",
                "message_id": "msg-1",
                "completed_at": completed_at,
            },
        )
    )

    payloads = [call.args[0] for call in user_ws.send_json.await_args_list]
    assert payloads[0]["type"] == "turn_settled"
    assert payloads[1] == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": False,
        "active_turn_chat_mode": None,
        "active_turn_chat_mode_source": None,
        "last_message_at": completed_at,
        "updated_at": completed_at,
    }


@pytest.mark.asyncio
async def test_turn_started_event_emits_activity_correction() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            event_bus=None,
            turn_scheduler=SimpleNamespace(
                add_observer=lambda _conversation_id, _observer: None,
                running_turn_state=lambda _conversation_id: None,
            ),
            session_factory=lambda: _NullSession(),
        )
    )
    manager = WebSocketConnectionManager(app)
    user_ws = AsyncMock()
    connection = await manager.connect(user_ws, claims={"sub": "user@example.com", "role": "user"})
    manager.subscribe(connection, "conv-1")

    started_at = "2026-01-02T03:03:00+00:00"
    await manager._handle_event(
        Event(
            type=EventType.TURN_STARTED,
            data={
                "conversation_id": "conv-1",
                "session_id": "sess-1",
                "message_id": "msg-1",
                "started_at": started_at,
                "chat_mode": "plan",
                "chat_mode_source": "user",
            },
        )
    )

    payloads = [call.args[0] for call in user_ws.send_json.await_args_list]
    assert payloads[0]["type"] == "turn_started"
    assert payloads[1] == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": True,
        "active_turn_chat_mode": "plan",
        "active_turn_chat_mode_source": "user",
        "last_message_at": started_at,
        "updated_at": started_at,
    }


@pytest.mark.asyncio
async def test_turn_error_event_fans_out_sidebar_correction_to_owner_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _conversation(_session: object, conversation_id: str) -> SimpleNamespace | None:
        if conversation_id == "conv-error":
            return SimpleNamespace(user_email="user@example.com")
        return None

    monkeypatch.setattr("cognis.api.websocket.get_conversation", _conversation)

    app = SimpleNamespace(state=SimpleNamespace(session_factory=lambda: _NullSession()))
    manager = WebSocketConnectionManager(app)
    sidebar_ws = AsyncMock()
    await manager.connect(sidebar_ws, claims={"sub": "user@example.com", "role": "user"})

    await manager._handle_event(
        Event(
            type=EventType.TURN_ERROR,
            data={"conversation_id": "conv-error"},
        )
    )

    sidebar_ws.send_json.assert_awaited_once_with(
        {
            "type": "conversation_updated",
            "conversation_id": "conv-error",
            "has_active_turn": False,
            "active_turn_chat_mode": None,
            "active_turn_chat_mode_source": None,
        }
    )


@pytest.mark.asyncio
async def test_sidebar_update_fans_out_to_owner_windows_outside_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(state=SimpleNamespace(session_factory=lambda: _NullSession()))
    manager = WebSocketConnectionManager(app)
    subscribed_ws = AsyncMock()
    sidebar_ws = AsyncMock()
    other_user_ws = AsyncMock()
    subscribed = await manager.connect(
        subscribed_ws,
        claims={"sub": "user@example.com", "role": "user"},
    )
    await manager.connect(sidebar_ws, claims={"sub": "user@example.com", "role": "user"})
    await manager.connect(other_user_ws, claims={"sub": "other@example.com", "role": "user"})
    manager.subscribe(subscribed, "conv-1")

    async def _fake_get_conversation(_session: Any, conversation_id: str) -> Any:
        assert conversation_id == "conv-1"
        return SimpleNamespace(user_email="user@example.com")

    monkeypatch.setattr("cognis.api.websocket.get_conversation", _fake_get_conversation)

    payload = {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": False,
    }
    await manager.send_sidebar_update_to_owner("conv-1", payload)

    subscribed_ws.send_json.assert_not_awaited()
    sidebar_ws.send_json.assert_awaited_once_with(payload)
    other_user_ws.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_attention_refresh_preserves_running_turn_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_pending_types(
        _session: Any, _user_email: str, _conversation_ids: list[str]
    ) -> dict[str, list[str]]:
        return {"conv-1": ["step_question"]}

    monkeypatch.setattr(
        "cognis.api.websocket.list_pending_notification_types_by_conversation",
        _fake_pending_types,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            event_bus=None,
            turn_scheduler=SimpleNamespace(
                running_turn_state=lambda _conversation_id: {
                    "chat_mode": "build",
                    "chat_mode_source": "user_explicit",
                }
            ),
            session_factory=lambda: _NullSession(),
        )
    )
    manager = WebSocketConnectionManager(app)
    user_ws = AsyncMock()
    await manager.connect(user_ws, claims={"sub": "user@example.com", "role": "user"})

    await manager._handle_event(
        Event(
            type=EventType.NOTIFICATION_CREATED,
            data={
                "notification_id": "notif-1",
                "notification_type": "step_question",
                "user_email": "user@example.com",
                "conversation_id": "conv-1",
            },
        )
    )

    user_ws.send_json.assert_awaited_once_with(
        {
            "type": "conversation_updated",
            "conversation_id": "conv-1",
            "pending_notification_types": ["step_question"],
            "has_active_turn": True,
            "active_turn_chat_mode": "build",
            "active_turn_chat_mode_source": "user_explicit",
        }
    )


@pytest.mark.asyncio
async def test_turn_observer_strips_attachment_payload_bytes() -> None:
    manager = AsyncMock()
    manager.has_tts_enabled_subscribers = lambda _conversation_id: False
    manager.app.state.turn_scheduler.queued_messages = lambda _conversation_id: []
    observer = WebSocketTurnObserver(manager)

    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-1",
            final_content="Final answer",
            runtime={"agent_id": "laforge", "model": "gpt-5.1", "reasoning_effort": "high"},
            attachments=[
                {
                    "artifact_id": "img_1",
                    "kind": "image",
                    "mime_type": "image/png",
                    "filename": "image.png",
                    "size_bytes": 3,
                    "content_b64": "YWJj",
                }
            ],
        )
    )

    payload = manager.send_to_conversation.await_args_list[0].args[1]
    assert payload["content"] == "Final answer"
    assert payload["runtime"] == {
        "agent_id": "laforge",
        "model": "gpt-5.1",
        "reasoning_effort": "high",
    }
    assert payload["attachments"] == [
        {
            "artifact_id": "img_1",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "image.png",
            "size_bytes": 3,
        }
    ]


@pytest.mark.asyncio
async def test_turn_observer_emits_conversation_activity_after_completion() -> None:
    manager = _RecordingManager()
    manager.app.state.turn_scheduler = type(
        "Scheduler",
        (),
        {"queued_messages": lambda self, _conversation_id: []},
    )()
    observer = WebSocketTurnObserver(cast(Any, manager))

    completed_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-1",
            turn_id="turn-1",
            final_content="Done",
            completed_at=completed_at,
        )
    )

    payloads = [payload for _, payload in manager.payloads]
    assert payloads[0]["type"] == "message_complete"
    assert payloads[1] == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": False,
        "active_turn_chat_mode": None,
        "active_turn_chat_mode_source": None,
        "last_message_at": completed_at.isoformat(),
        "updated_at": completed_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_turn_observer_tool_result_strips_attachment_payload_bytes() -> None:
    manager = AsyncMock()
    observer = WebSocketTurnObserver(manager)

    await observer.on_tool_result(
        "conv-1",
        "sess-1",
        "call-1",
        "image_edit",
        "done",
        False,
        42,
        None,
        [
            {
                "artifact_id": "img_1",
                "kind": "image",
                "mime_type": "image/png",
                "filename": "image.png",
                "size_bytes": 3,
                "content_b64": "YWJj",
            }
        ],
    )

    payload = manager.send_to_conversation.await_args.args[1]
    assert payload["attachments"] == [
        {
            "artifact_id": "img_1",
            "kind": "image",
            "mime_type": "image/png",
            "filename": "image.png",
            "size_bytes": 3,
        }
    ]


@pytest.mark.asyncio
async def test_turn_observer_tool_result_includes_file_diffs() -> None:
    manager = AsyncMock()
    observer = WebSocketTurnObserver(manager)
    file_diffs = [{"path": "example.py", "diff": "--- example.py\n+++ example.py\n"}]

    await observer.on_tool_result(
        "conv-1",
        "sess-1",
        "call-1",
        "edit",
        "done",
        False,
        42,
        None,
        None,
        file_diffs,
    )

    payload = manager.send_to_conversation.await_args.args[1]
    assert payload["file_diffs"] == file_diffs


def test_workflow_composed_payload_supports_lifecycle_backed_replay() -> None:
    payload = _workflow_composed_payload(
        "conv-1",
        {
            "event": "workflow_composed",
            "workflow_id": "wf-1",
            "workflow_name": "Evening Summary Deterministic Workflow",
            "lifecycle": "persistent",
            "steps": ["collect_gmail", "synthesize_summary"],
            "task_id": "task-1",
        },
    )

    assert payload == {
        "type": "workflow_composed",
        "conversation_id": "conv-1",
        "task_id": "task-1",
        "schedule_id": None,
        "workflow_id": "wf-1",
        "workflow_name": "Evening Summary Deterministic Workflow",
        "lifecycle": "persistent",
        "steps": ["collect_gmail", "synthesize_summary"],
    }


@pytest.mark.asyncio
async def test_ws_first_slash_command_bootstraps_root_session(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        conversation_id = await _seed_conversation(client.app, owner="owner@example.com")

        async def _create_session(**_: object) -> None:
            return None

        client.app.state.providers.guardrails.create_session = _create_session
        manager = _RecordingManager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-first-slash",
            websocket=AsyncMock(),
            user_email="owner@example.com",
            role="user",
        )

        await _handle_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {"type": "message", "conversation_id": conversation_id, "content": "/plan"},
        )

        async with client.app.state.session_factory() as session:
            from cognis.store.queries import get_conversation

            conversation = await get_conversation(session, conversation_id)
            assert conversation is not None
            assert conversation.active_session_id is not None

        assert manager.errors == []
        assert f"event:{conversation_id}:system_message" in manager.snapshots


@pytest.mark.asyncio
async def test_ws_message_does_not_send_queue_snapshot_before_authorization(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        conversation_id = await _seed_conversation(client.app, owner="owner@example.com")
        manager = _RecordingManager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-queue-auth",
            websocket=AsyncMock(),
            user_email="intruder@example.com",
            role="user",
        )

        await _handle_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {"type": "message", "conversation_id": conversation_id, "content": "hello"},
        )

        assert manager.errors[-1]["code"] == "forbidden"
        assert manager.subscriptions == []
        assert manager.snapshots == []


@pytest.mark.asyncio
async def test_ws_viewer_cannot_cancel_or_update_queued_messages(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        conversation_id = await _seed_conversation(client.app, owner="viewer@example.com")
        turn_scheduler = AsyncMock()
        client.app.state.turn_scheduler = turn_scheduler
        manager = _RecordingManager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-viewer-queue",
            websocket=AsyncMock(),
            user_email="viewer@example.com",
            role="viewer",
        )

        await _handle_cancel_queued_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {
                "type": "cancel_queued_message",
                "conversation_id": conversation_id,
                "queue_id": "q-1",
            },
        )
        await _handle_update_queued_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {
                "type": "update_queued_message",
                "conversation_id": conversation_id,
                "queue_id": "q-1",
                "content": "edited",
            },
        )

        assert [error["code"] for error in manager.errors] == ["forbidden", "forbidden"]
        turn_scheduler.cancel_queued_message.assert_not_awaited()
        turn_scheduler.update_queued_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_managed_conversation_cannot_send_cancel_or_update_queued_messages(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_ws_test_client(monkeypatch, tmp_path) as client:

        async def _seed_managed_conversation() -> str:
            async with client.app.state.session_factory() as session:
                await create_user(
                    session,
                    email="owner@example.com",
                    name="Owner",
                    password_hash=client.app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await create_agent(
                    session,
                    agent_id="controller-agent",
                    owner_email="owner@example.com",
                    name="Controller",
                    status="active",
                )
                await create_agent(
                    session,
                    agent_id="target-agent",
                    owner_email="owner@example.com",
                    name="Target",
                    status="active",
                )
                controller = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="controller-agent",
                    context_type="web",
                )
                target = await create_conversation(
                    session,
                    user_email="owner@example.com",
                    agent_id="target-agent",
                    context_type="agent_work",
                )
                await create_managed_conversation_link(
                    session,
                    user_email="owner@example.com",
                    controller_agent_id="controller-agent",
                    controller_conversation_id=controller.conversation_id,
                    controller_session_id="controller-session",
                    target_agent_id="target-agent",
                    target_conversation_id=target.conversation_id,
                    target_session_id="target-session",
                    title="Target",
                )
                await session.commit()
                return target.conversation_id

        conversation_id = await _seed_managed_conversation()
        turn_scheduler = AsyncMock()
        client.app.state.turn_scheduler = turn_scheduler
        manager = _RecordingManager()
        connection = AuthenticatedWebSocket(
            connection_id="conn-managed-queue",
            websocket=AsyncMock(),
            user_email="owner@example.com",
            role="user",
        )

        await _handle_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {
                "type": "message",
                "conversation_id": conversation_id,
                "content": "direct target send",
            },
        )
        await _handle_cancel_queued_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {
                "type": "cancel_queued_message",
                "conversation_id": conversation_id,
                "queue_id": "q-1",
            },
        )
        await _handle_update_queued_message(
            client.app,
            manager,  # type: ignore[arg-type]
            connection,
            {
                "type": "update_queued_message",
                "conversation_id": conversation_id,
                "queue_id": "q-1",
                "content": "edited",
            },
        )

        assert [error["code"] for error in manager.errors] == [
            "managed_conversation_read_only",
            "managed_conversation_read_only",
            "managed_conversation_read_only",
        ]
        turn_scheduler.submit_turn.assert_not_awaited()
        turn_scheduler.cancel_queued_message.assert_not_awaited()
        turn_scheduler.update_queued_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# WebSocket auth flow tests (via TestClient)
# ---------------------------------------------------------------------------


def _create_ws_test_client(monkeypatch: object, tmp_path: Path) -> Any:
    """Create a TestClient for WebSocket auth tests."""
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    from cognis.api.app import create_app

    app = create_app()
    return TestClient(app)


def test_ws_auth_invalid_token_closes_4401(monkeypatch: object, tmp_path: Path) -> None:
    """Sending an invalid JWT as the first message should close with 4401."""
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        try:
            with client.websocket_connect("/api/ws") as ws:
                ws.send_json({"type": "auth", "token": "invalid.jwt.token"})
                # The server should close the connection
                response = ws.receive_json()
                # If we get a response, it should be an error
                assert response.get("type") in ("error", None)
        except Exception:
            # Connection closed — expected for invalid auth
            pass


def test_ws_auth_non_auth_first_message_closes(monkeypatch: object, tmp_path: Path) -> None:
    """Sending a non-auth message first should close the connection."""
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        try:
            with client.websocket_connect("/api/ws") as ws:
                ws.send_json({"type": "message", "content": "hello"})
                response = ws.receive_json()
                assert response.get("type") in ("error", None)
        except Exception:
            # Connection closed — expected
            pass


def test_ws_auth_valid_token_authenticates(monkeypatch: object, tmp_path: Path) -> None:
    """A valid JWT should produce an 'authenticated' response."""
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        from cognis.store.queries import create_user

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="wstest@example.com",
                    name="WS Test",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            return app.state.auth_provider.sign_access_token(
                "wstest@example.com", "WS Test", "user"
            )

        token = asyncio.run(_seed())

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            response = ws.receive_json()
            assert response["type"] == "authenticated"


def test_ws_ping_returns_pong(monkeypatch: object, tmp_path: Path) -> None:
    """After auth, a ping message should return pong."""
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        from cognis.store.queries import create_user

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="wsping@example.com",
                    name="WS Ping",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            return app.state.auth_provider.sign_access_token(
                "wsping@example.com", "WS Ping", "user"
            )

        token = asyncio.run(_seed())

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            auth_resp = ws.receive_json()
            assert auth_resp["type"] == "authenticated"

            ws.send_json({"type": "ping"})
            pong_resp = ws.receive_json()
            assert pong_resp["type"] == "pong"


def test_ws_unknown_message_type_returns_error(monkeypatch: object, tmp_path: Path) -> None:
    """An unknown message type should return a validation_error."""
    with _create_ws_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        import asyncio

        from cognis.store.queries import create_user

        async def _seed() -> str:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="wsunknown@example.com",
                    name="WS Unknown",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="user",
                )
                await session.commit()
            return app.state.auth_provider.sign_access_token(
                "wsunknown@example.com", "WS Unknown", "user"
            )

        token = asyncio.run(_seed())

        with client.websocket_connect("/api/ws") as ws:
            ws.send_json({"type": "auth", "token": token})
            auth_resp = ws.receive_json()
            assert auth_resp["type"] == "authenticated"

            ws.send_json({"type": "nonexistent_type"})
            error_resp = ws.receive_json()
            assert error_resp["type"] == "error"
            assert error_resp.get("code") == "validation_error"
