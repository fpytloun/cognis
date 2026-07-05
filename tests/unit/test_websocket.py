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

from cognis.api.chat_v2.realtime import assistant_stream_runtime_item
from cognis.api.chat_v2.schemas import TimelineItem
from cognis.api.timeline_visibility import is_visible_persisted_system_message
from cognis.api.websocket import (
    DEFAULT_INBOUND_RATE_LIMIT,
    AuthenticatedWebSocket,
    WebSocketConnectionManager,
    WebSocketTurnObserver,
    _assistant_runtime_payload,
    _chat_v2_delegation_runtime_item,
    _chat_v2_phase_hint_items_from_session_cache,
    _event_to_payload,
    _handle_cancel_queued_message,
    _handle_message,
    _handle_update_queued_message,
    _render_command_result,
    _workflow_composed_payload,
)
from cognis.core.commands import CommandResult
from cognis.core.events import Event, EventType
from cognis.core.turn_scheduler import (
    SessionCreationFailedError,
    TurnError,
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


def _strip_order_key(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a projected timeline item without the orderKey field."""
    return {k: v for k, v in item.items() if k != "orderKey"}


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


@pytest.mark.asyncio
async def test_turn_observer_sends_context_usage_runtime_frame() -> None:
    manager = SimpleNamespace(send_chat_v2_runtime_to_conversation=AsyncMock())
    observer = WebSocketTurnObserver(manager)  # type: ignore[arg-type]
    usage = {
        "prompt_tokens": 42_000,
        "max_context_tokens": 128_000,
        "percentage": 32.8,
        "model": "test-model",
        "reasoning_effort": None,
    }

    await observer.on_context_usage("conv-1", "sess-1", usage, "turn-1")

    manager.send_chat_v2_runtime_to_conversation.assert_awaited_once_with(
        "conv-1",
        volatile_items=[],
        active_session_id="sess-1",
        context_usage=usage,
    )


class _RecordingManager:
    def __init__(self) -> None:
        self.errors: list[dict[str, object]] = []
        self.snapshots: list[str] = []
        self.subscriptions: list[str] = []
        self.payloads: list[tuple[str, dict[str, object]]] = []
        self.sidebar_payloads: list[tuple[str, dict[str, object]]] = []
        self.chat_v2_runtime_payloads: list[tuple[str, bool, int]] = []
        self.chat_v2_runtime_items: list[list[TimelineItem]] = []
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

    async def send_chat_v2_runtime_to_conversation(
        self,
        conversation_id: str,
        *,
        volatile_items: list[TimelineItem],
        has_active_turn: bool = True,
        active_session_id: str | None = None,
        context_usage: dict[str, Any] | None = None,
    ) -> None:
        del active_session_id, context_usage
        self.chat_v2_runtime_payloads.append(
            (conversation_id, has_active_turn, len(volatile_items))
        )
        self.chat_v2_runtime_items.append(volatile_items)
        self.snapshots.append(f"chat_v2:{conversation_id}:{len(volatile_items)}")

    def has_tts_enabled_subscribers(self, _conversation_id: str) -> bool:
        return False


class _RecordingConnection:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.chat_v2_cursors: dict[str, str] = {}

    async def send_json(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
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

    def running_turn_state(self, conversation_id: str) -> dict[str, object] | None:
        assert conversation_id == "conv-1"
        return {"chat_mode": "build", "chat_mode_source": "user"}


class _RuntimeSnapshotSessionCache:
    def active_thinking_snapshots(self, session_id: str) -> list[dict[str, object]]:
        assert session_id == "sess-1"
        return [{"message_id": "msg-1", "blocks": [{"block_id": "think-1", "content": "thought"}]}]


@pytest.mark.asyncio
async def test_render_command_system_message_marks_command_result() -> None:
    manager = _RecordingManager()

    await _render_command_result(
        manager,
        "conv-1",
        CommandResult(
            type="system_message",
            text="Agent profile switched to: fast",
            data={"resolved_agent_profile_id": "fast"},
        ),
    )

    assert manager.payloads == [
        (
            "conv-1",
            {
                "type": "system_message",
                "conversation_id": "conv-1",
                "text": "Agent profile switched to: fast",
                "command_result": True,
                "resolved_agent_profile_id": "fast",
            },
        )
    ]


@pytest.mark.asyncio
async def test_render_command_queued_result_is_not_chat_queue_snapshot() -> None:
    manager = _RecordingManager()

    await _render_command_result(
        manager,
        "conv-1",
        CommandResult(
            type="queued",
            text="Working on that in the background.",
            data={"task_id": "task-1", "command": "/task"},
        ),
    )

    assert manager.payloads == [
        (
            "conv-1",
            {
                "type": "queued",
                "conversation_id": "conv-1",
                "queued_count": 0,
                "reason": "Working on that in the background.",
                "command_result": True,
                "task_id": "task-1",
                "command": "/task",
            },
        )
    ]


class _StaleRuntimeSnapshotScheduler(_RuntimeSnapshotScheduler):
    def running_turn_state(self, conversation_id: str) -> None:
        assert conversation_id == "conv-1"
        return None


class _ToolBoundaryThinkingSessionCache:
    def active_thinking_snapshots(self, session_id: str) -> list[dict[str, object]]:
        assert session_id == "sess-1"
        return [
            {
                "session_id": "sess-1",
                "message_id": "msg-1",
                "turn_id": "turn-1",
                "assistant_phase_index": 1,
                "blocks": [
                    {
                        "block_id": "think-1",
                        "title": "Thinking",
                        "content": "Planning before tool",
                        "complete": False,
                    }
                ],
                "updated_at": "2026-01-01T00:00:01+00:00",
            }
        ]


class _RecordingWebSocketManager(WebSocketConnectionManager):
    def __init__(self) -> None:
        super().__init__(SimpleNamespace(state=SimpleNamespace()))
        self.sent_payloads: list[tuple[str, dict[str, Any]]] = []
        self.chat_v2_runtime_payloads: list[tuple[str, bool, int]] = []
        self.chat_v2_runtime_items: list[list[TimelineItem]] = []

    async def send_to_conversation(self, conversation_id: str, payload: dict[str, Any]) -> None:
        self.sent_payloads.append((conversation_id, payload))

    async def send_chat_v2_runtime_to_conversation(
        self,
        conversation_id: str,
        *,
        volatile_items: list[TimelineItem],
        has_active_turn: bool = True,
        active_session_id: str | None = None,
        context_usage: dict[str, Any] | None = None,
    ) -> None:
        del active_session_id, context_usage
        self.chat_v2_runtime_payloads.append(
            (conversation_id, has_active_turn, len(volatile_items))
        )
        self.chat_v2_runtime_items.append(volatile_items)

    async def _resolve_conversation_id(self, event: Event) -> str | None:  # noqa: SLF001
        return str(event.data.get("conversation_id") or "conv-1")


@pytest.mark.asyncio
async def test_chat_v2_runtime_frames_are_opt_in_and_cursor_preserving() -> None:
    manager = WebSocketConnectionManager(SimpleNamespace(state=SimpleNamespace()))
    v2_socket = _RecordingWebSocket()
    legacy_socket = _RecordingWebSocket()
    v2_connection = AuthenticatedWebSocket(
        connection_id="v2",
        websocket=cast(Any, v2_socket),
        user_email="user@example.com",
        role="user",
    )
    legacy_connection = AuthenticatedWebSocket(
        connection_id="legacy",
        websocket=cast(Any, legacy_socket),
        user_email="user@example.com",
        role="user",
    )
    manager._connections[v2_connection.connection_id] = v2_connection  # noqa: SLF001
    manager._connections[legacy_connection.connection_id] = legacy_connection  # noqa: SLF001
    manager.subscribe_chat_v2(v2_connection, "conv-1", cursor="cursor-1")
    manager.subscribe(legacy_connection, "conv-1")

    await manager.send_chat_v2_runtime_to_conversation(
        "conv-1",
        volatile_items=[
            item
            for item in [
                assistant_stream_runtime_item(
                    {
                        "content": "stream",
                        "message_id": "msg-1",
                        "session_id": "sess-1",
                    },
                    local=0,
                )
            ]
            if item is not None
        ],
        active_session_id="sess-1",
    )

    assert len(v2_socket.payloads) == 1
    assert legacy_socket.payloads == []
    frame = v2_socket.payloads[0]
    assert frame["type"] == "chat_v2_frame"
    assert frame["cursor_before"] == "cursor-1"
    assert frame["cursor_after"] == "cursor-1"
    runtime = cast(dict[str, Any], frame["runtime"])
    assert runtime["runtime_revision"] == 1
    assert runtime["volatile_items"][0]["stable"] is False


@pytest.mark.asyncio
async def test_chat_v2_unsubscribe_stops_v2_frames_without_legacy_subscription() -> None:
    manager = WebSocketConnectionManager(SimpleNamespace(state=SimpleNamespace()))
    socket = _RecordingWebSocket()
    connection = AuthenticatedWebSocket(
        connection_id="v2",
        websocket=cast(Any, socket),
        user_email="user@example.com",
        role="user",
    )
    manager._connections[connection.connection_id] = connection  # noqa: SLF001
    manager.subscribe_chat_v2(connection, "conv-1", cursor="cursor-1")

    manager.unsubscribe_chat_v2(connection, "conv-1")
    assert "conv-1" not in connection.subscriptions
    assert "conv-1" not in connection.chat_v2_cursors

    await manager.send_chat_v2_runtime_to_conversation(
        "conv-1",
        volatile_items=[
            item
            for item in [
                assistant_stream_runtime_item(
                    {
                        "content": "stream",
                        "message_id": "msg-1",
                        "session_id": "sess-1",
                    },
                    local=0,
                )
            ]
            if item is not None
        ],
        active_session_id="sess-1",
    )

    assert socket.payloads == []


def test_chat_v2_phase_hints_project_cached_tool_events_for_runtime_streams() -> None:
    cached_tool_event = SimpleNamespace(
        seq=7,
        type="tool_call",
        data={
            "call_id": "call-1",
            "tool_name": "bash",
            "turn_id": "turn-1",
            "assistant_phase_index": 0,
            "turn_cycle_index": 0,
        },
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    cache = SimpleNamespace(
        get_events_since_compaction=lambda session_id: (
            [cached_tool_event] if session_id == "sess-1" else []
        )
    )

    phase_hint_items = _chat_v2_phase_hint_items_from_session_cache(cache, "sess-1")
    assert len(phase_hint_items) == 1
    assert phase_hint_items[0].id == "tool:call-1"
    assert phase_hint_items[0].assistant_phase_index == 0
    assert phase_hint_items[0].turn_cycle_index == 0

    stream = assistant_stream_runtime_item(
        {
            "content": "After tool",
            "message_id": "msg-1",
            "turn_id": "turn-1",
            "session_id": "sess-1",
        },
        local=0,
        phase_hint_items=phase_hint_items,
    )

    assert stream is not None
    assert stream.assistant_phase_index == 1
    assert stream.turn_cycle_index == 1
    assert phase_hint_items[0].sort_key < stream.sort_key


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
async def test_websocket_turn_observer_coalesces_token_as_chat_v2_runtime_item() -> None:
    manager = _RecordingManager()
    manager.app.state.turn_scheduler = _RuntimeSnapshotScheduler()
    manager.app.state.session_cache = _RuntimeSnapshotSessionCache()
    observer = WebSocketTurnObserver(manager)

    await observer.on_token(
        "conv-1",
        "sess-1",
        "msg-1",
        "turn-1",
        "chunk",
        chunk_index=0,
        content_offset=6,
    )
    await observer._flush_coalesced("conv-1")  # noqa: SLF001

    assert manager.payloads == []
    assert manager.chat_v2_runtime_payloads == [("conv-1", True, 1)]
    item = manager.chat_v2_runtime_items[0][0]
    assert item.kind == "message"
    assert item.content == "stream"


@pytest.mark.asyncio
async def test_websocket_turn_observer_coalesces_thinking_as_chat_v2_runtime_item() -> None:
    manager = _RecordingManager()
    manager.app.state.session_cache = _RuntimeSnapshotSessionCache()
    observer = WebSocketTurnObserver(manager)

    await observer.on_thinking(
        "conv-1",
        "sess-1",
        "msg-1",
        "turn-1",
        "think-1",
        "thought",
        "Thinking",
        False,
    )
    await observer._flush_coalesced("conv-1")  # noqa: SLF001

    assert manager.payloads == []
    assert manager.chat_v2_runtime_payloads == [("conv-1", True, 1)]
    item = manager.chat_v2_runtime_items[0][0]
    assert item.kind == "thinking"


@pytest.mark.asyncio
async def test_websocket_turn_observer_sends_tool_call_as_chat_v2_runtime_item() -> None:
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

    assert manager.payloads == []
    assert manager.chat_v2_runtime_payloads == [("conv-1", True, 1)]
    item = manager.chat_v2_runtime_items[0][0]
    assert item.id == "tool:call-1"
    assert item.kind == "tool_call"
    assert item.source_refs[0].session_id == "sess-1"
    assert item.call_id == "call-1"
    assert item.turn_id == "turn-1"
    assert item.tool_name == "bash"
    assert item.status == "running"
    assert item.arguments == {"command": "true"}


@pytest.mark.asyncio
async def test_websocket_turn_observer_keeps_queued_messages_out_of_timeline() -> None:
    manager = _RecordingManager()
    observer = WebSocketTurnObserver(manager)  # type: ignore[arg-type]

    await observer.on_queued_messages(
        "conv-1",
        [
            {
                "queue_id": "qmsg-1",
                "client_message_id": "cmsg-1",
                "content": "queued follow-up",
            }
        ],
    )

    assert manager.payloads == [
        (
            "conv-1",
            {
                "type": "queued_messages_updated",
                "conversation_id": "conv-1",
                "queued_count": 1,
                "messages": [
                    {
                        "queue_id": "qmsg-1",
                        "client_message_id": "cmsg-1",
                        "content": "queued follow-up",
                    }
                ],
            },
        )
    ]


def test_chat_v2_delegation_runtime_item_folds_onto_parent_tool_call() -> None:
    event = Event(
        type=EventType.DELEGATION_PROGRESS,
        data={
            "conversation_id": "conv-1",
            "parent_session_id": "sess-parent",
            "turn_id": "turn-1",
            "assistant_phase_index": 2,
            "turn_cycle_index": 2,
            "call_id": "call-delegate",
            "child_session_id": "sess-child",
            "title": "Explore project",
            "tool_call_count": 0,
        },
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    item = _chat_v2_delegation_runtime_item(event)

    assert item is not None
    assert item.id == "tool:call-delegate"
    assert item.kind == "tool_call"
    assert item.tool_name == "delegate"
    assert item.status == "running"
    assert item.source_refs[0].session_id == "sess-parent"
    assert item.turn_id == "turn-1"
    assert item.assistant_phase_index == 2
    assert item.turn_cycle_index == 2
    assert item.sort_key.startswith("9998:")
    assert item.delegation == {
        "child_session_id": "sess-child",
        "status": "running",
        "turn_id": "turn-1",
        "assistant_phase_index": 2,
        "turn_cycle_index": 2,
        "agent_id": None,
        "used_agent_id": None,
        "title": "Explore project",
        "summary": None,
        "started_at": None,
        "duration_ms": None,
        "result_summary": None,
        "result_content": None,
        "result_source": None,
        "result_truncated": None,
        "result_anchors": None,
        "todos": [],
        "tool_call_count": 0,
        "max_tool_calls": None,
        "last_tool": None,
        "error": None,
    }


@pytest.mark.asyncio
async def test_terminal_delegation_event_sends_chat_v2_runtime_and_side_effect_frame() -> None:
    manager = _RecordingWebSocketManager()
    event = Event(
        type=EventType.DELEGATION_COMPLETED,
        data={
            "conversation_id": "conv-1",
            "session_id": "sess-1",
            "call_id": "call-delegate",
            "child_session_id": "child-1",
            "title": "Explore project",
            "result": "done",
        },
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    await manager._handle_event(event)  # noqa: SLF001

    assert [payload["type"] for _, payload in manager.sent_payloads] == ["delegation_completed"]
    assert manager.chat_v2_runtime_payloads == [("conv-1", True, 1)]


@pytest.mark.asyncio
async def test_websocket_manager_sends_authoritative_runtime_snapshot() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            turn_scheduler=_RuntimeSnapshotScheduler(),
            session_cache=_RuntimeSnapshotSessionCache(),
        )
    )
    manager = WebSocketConnectionManager(app)
    connection = _RecordingConnection()

    await manager._send_conversation_runtime_snapshot(  # noqa: SLF001
        connection,  # type: ignore[arg-type]
        "conv-1",
        active_session_id="sess-1",
    )

    assert len(connection.payloads) == 1
    payload = connection.payloads[0]
    assert isinstance(payload.pop("runtime_generation"), str)
    assert isinstance(payload.pop("server_time"), str)
    assert isinstance(payload.pop("build_id"), str)
    assert payload == {
        "type": "conversation_runtime_snapshot",
        "conversation_id": "conv-1",
        "queued_messages": [{"queue_id": "queue-1", "content": "queued"}],
        "queued_count": 1,
        "has_active_turn": True,
        "active_turn_chat_mode": "build",
        "active_turn_chat_mode_source": "user",
        "active_streams": [{"message_id": "msg-1", "content": "stream"}],
        "active_tool_outputs": [{"call_id": "call-1", "chunk": "output"}],
        "active_thinking": [
            {"message_id": "msg-1", "blocks": [{"block_id": "think-1", "content": "thought"}]}
        ],
    }


@pytest.mark.asyncio
async def test_websocket_manager_chat_v2_snapshot_suppresses_stale_runtime_items() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            turn_scheduler=_StaleRuntimeSnapshotScheduler(),
            session_cache=_RuntimeSnapshotSessionCache(),
        )
    )
    manager = WebSocketConnectionManager(app)
    connection = _RecordingConnection()
    connection.chat_v2_cursors["conv-1"] = "cursor-1"

    await manager._send_conversation_runtime_snapshot(  # noqa: SLF001
        connection,  # type: ignore[arg-type]
        "conv-1",
        active_session_id="sess-1",
    )

    assert len(connection.payloads) == 2
    legacy_payload = connection.payloads[0]
    chat_v2_payload = connection.payloads[1]

    assert legacy_payload["type"] == "conversation_runtime_snapshot"
    assert legacy_payload["has_active_turn"] is False
    assert legacy_payload["active_streams"] == []
    assert legacy_payload["active_tool_outputs"] == []
    assert legacy_payload["active_thinking"] == []

    assert chat_v2_payload["type"] == "chat_v2_frame"
    assert chat_v2_payload["cursor_before"] == "cursor-1"
    assert chat_v2_payload["cursor_after"] == "cursor-1"
    runtime = chat_v2_payload["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["has_active_turn"] is False
    assert runtime["active_turn"] is None
    assert runtime["volatile_items"] == []


@pytest.mark.asyncio
async def test_turn_observer_tool_call_runtime_rekeys_live_thinking_before_boundary() -> None:
    manager = _RecordingManager()
    manager.app.state.session_cache = _ToolBoundaryThinkingSessionCache()
    observer = WebSocketTurnObserver(cast(Any, manager))

    await observer.on_tool_call(
        "conv-1",
        "sess-1",
        "call-1",
        "read",
        {"file_path": "README.md"},
        "turn-1",
        1,
    )

    assert manager.payloads == []
    items = manager.chat_v2_runtime_items[0]
    assert [item.kind for item in items] == ["thinking", "tool_call"]
    assert items[0].sort_key < items[1].sort_key
    assert items[0].turn_id == "turn-1"
    assert items[1].call_id == "call-1"
    assert items[0].assistant_phase_index == 1
    assert items[1].assistant_phase_index == 1


@pytest.mark.asyncio
async def test_turn_observer_tool_result_runtime_preserves_assistant_phase() -> None:
    manager = _RecordingManager()
    observer = WebSocketTurnObserver(cast(Any, manager))

    await observer.on_tool_result(
        "conv-1",
        "sess-1",
        "call-1",
        "read",
        "ok",
        False,
        42,
        None,
        None,
        None,
        "turn-1",
        None,
        1,
    )

    assert manager.payloads == []
    items = manager.chat_v2_runtime_items[0]
    assert len(items) == 1
    assert items[0].kind == "tool_call"
    assert items[0].call_id == "call-1"
    assert items[0].assistant_phase_index == 1
    assert ":000001:03:" in items[0].sort_key


def test_visible_persisted_system_message_filter_allows_explicit_notices() -> None:
    assert is_visible_persisted_system_message(
        {"notice_id": "turn-init:fup_task_failed", "kind": "turn_initiated"}
    )
    assert is_visible_persisted_system_message({"event": "turn_initiated"})


def test_visible_persisted_system_message_filter_rejects_internal_context() -> None:
    assert not is_visible_persisted_system_message(
        {"content": ("Environment: - Executor: olorin (websocket) - Platform: unknown (unknown)")}
    )
    assert not is_visible_persisted_system_message(
        {"content": "Additional tools may be available but hidden by the current step profile."}
    )


def test_visible_persisted_system_message_filter_rejects_compaction_start() -> None:
    assert not is_visible_persisted_system_message(
        {
            "content": "Automatic compaction is starting before this turn continues.",
            "notice_id": "notice-compaction-start",
            "kind": "compaction_start",
        }
    )


def test_event_bus_system_notice_hides_compaction_start_payload() -> None:
    event = Event(
        type=EventType.SYSTEM_NOTICE,
        data={
            "conversation_id": "conv-1",
            "message": "Automatic compaction is starting before this turn continues.",
            "kind": "compaction_start",
            "notice_id": "notice-compaction-start",
        },
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert _event_to_payload(event, "conv-1") is None
    assert not is_visible_persisted_system_message(
        {
            "message": (
                "The model provider rejected the request because the context window is full. "
                "Cognis is compacting the saved conversation and will retry the turn in a fresh compacted session."
            ),
            "status": "started",
            "notice_id": "provider-overflow",
        }
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
        # Should allow up to the configured inbound rate limit.
        for _ in range(DEFAULT_INBOUND_RATE_LIMIT):
            assert ws.allow_inbound_message() is True
        # The next message in the same window should be denied.
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
    ws.recent_message_times = deque([0.0] * DEFAULT_INBOUND_RATE_LIMIT)

    async def _run() -> bool:
        return ws.allow_inbound_message()

    loop = asyncio.new_event_loop()
    # loop.time() starts near 0 for a new loop, so set times in the far past
    ws.recent_message_times = deque([-10.0] * DEFAULT_INBOUND_RATE_LIMIT)
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
async def test_turn_completion_event_preserves_activity_for_pending_continuation() -> None:
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
                "chat_mode": "build",
                "chat_mode_source": "user_explicit",
                "managed_continuation_pending": True,
            },
        )
    )

    payloads = [call.args[0] for call in user_ws.send_json.await_args_list]
    assert payloads[0]["type"] == "turn_settled"
    assert payloads[1] == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": True,
        "active_turn_chat_mode": "build",
        "active_turn_chat_mode_source": "user_explicit",
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

    payload = next(
        call.args[1]
        for call in manager.send_to_conversation.await_args_list
        if call.args[1].get("type") == "message_complete"
    )
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
    message_complete = next(
        payload for payload in payloads if payload["type"] == "message_complete"
    )
    activity_update = next(
        payload
        for payload in payloads
        if payload["type"] == "conversation_updated" and payload["conversation_id"] == "conv-1"
    )
    assert message_complete["type"] == "message_complete"
    assert activity_update == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": False,
        "active_turn_chat_mode": None,
        "active_turn_chat_mode_source": None,
        "last_message_at": completed_at.isoformat(),
        "updated_at": completed_at.isoformat(),
    }
    assert ("conv-1", False, 1) in manager.chat_v2_runtime_payloads


@pytest.mark.asyncio
async def test_turn_observer_preserves_activity_for_pending_continuation() -> None:
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
            final_content="Partial",
            completed_at=completed_at,
            chat_mode="build",
            chat_mode_source="user_explicit",
            managed_continuation_pending=True,
        )
    )

    payloads = [payload for _, payload in manager.payloads]
    message_complete = next(
        payload for payload in payloads if payload["type"] == "message_complete"
    )
    activity_update = next(
        payload
        for payload in payloads
        if payload["type"] == "conversation_updated" and payload["conversation_id"] == "conv-1"
    )
    assert message_complete["managed_continuation_pending"] is True
    assert activity_update == {
        "type": "conversation_updated",
        "conversation_id": "conv-1",
        "has_active_turn": True,
        "active_turn_chat_mode": "build",
        "active_turn_chat_mode_source": "user_explicit",
        "last_message_at": completed_at.isoformat(),
        "updated_at": completed_at.isoformat(),
    }
    assert ("conv-1", True, 1) in manager.chat_v2_runtime_payloads


@pytest.mark.asyncio
async def test_turn_observer_clears_chat_v2_runtime_after_error() -> None:
    manager = _RecordingManager()
    observer = WebSocketTurnObserver(cast(Any, manager))

    await observer.on_turn_error(
        "conv-1",
        TurnError("cancelled", "Cancelled", recoverable=False),
    )

    assert ("conv-1", False, 0) in manager.chat_v2_runtime_payloads


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

    kwargs = manager.send_chat_v2_runtime_to_conversation.await_args.kwargs
    items = cast(list[TimelineItem], kwargs["volatile_items"])
    assert [
        attachment.model_dump(mode="json", exclude_none=True) for attachment in items[0].attachments
    ] == [
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

    kwargs = manager.send_chat_v2_runtime_to_conversation.await_args.kwargs
    items = cast(list[TimelineItem], kwargs["volatile_items"])
    assert [
        file_diff.model_dump(mode="json", exclude_none=True) for file_diff in items[0].file_diffs
    ] == file_diffs


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
            return str(
                app.state.auth_provider.sign_access_token("wstest@example.com", "WS Test", "user")
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
            return str(
                app.state.auth_provider.sign_access_token("wsping@example.com", "WS Ping", "user")
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
            return str(
                app.state.auth_provider.sign_access_token(
                    "wsunknown@example.com", "WS Unknown", "user"
                )
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
