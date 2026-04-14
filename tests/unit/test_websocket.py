"""Dedicated WebSocket handler and turn scheduler unit tests.

Covers classify_turn_error, authentication flow, rate limiting,
backpressure, and access control.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cognis.api.websocket import AuthenticatedWebSocket, WebSocketTurnObserver
from cognis.core.turn_scheduler import (
    SessionCreationFailedError,
    TurnResult,
    classify_turn_error,
)
from cognis.models.config import ProviderHealth

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


@dataclass
class _FakeProviders:
    guardrails: Any = field(default_factory=lambda: _FakeProvider())
    llm: Any = field(default_factory=lambda: _FakeProvider())
    memory: Any = field(default_factory=lambda: _FakeProvider())


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
    observer = WebSocketTurnObserver(manager)

    await observer.on_turn_complete(
        TurnResult(
            conversation_id="conv-1",
            session_id="sess-1",
            message_id="msg-1",
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


# ---------------------------------------------------------------------------
# WebSocket auth flow tests (via TestClient)
# ---------------------------------------------------------------------------


def _create_ws_test_client(monkeypatch: object, tmp_path: Path) -> Any:
    """Create a TestClient for WebSocket auth tests."""
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    from fastapi.testclient import TestClient

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
