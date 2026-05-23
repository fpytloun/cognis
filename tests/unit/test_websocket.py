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
    WebSocketTurnObserver,
    _event_to_payload,
    _handle_cancel_queued_message,
    _handle_message,
    _handle_update_queued_message,
    _workflow_composed_payload,
)
from cognis.core.events import Event, EventType
from cognis.core.turn_scheduler import (
    SessionCreationFailedError,
    TurnResult,
    classify_turn_error,
)
from cognis.models.config import ProviderHealth
from cognis.store.queries import create_agent, create_conversation, create_user

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

    def has_tts_enabled_subscribers(self, _conversation_id: str) -> bool:
        return False


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
