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
    _decode_messages_cursor,
    _messages_cursor_anchor_event,
)
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_user,
    get_conversation,
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


def test_messages_cursor_anchor_event_skips_history_gap_seq_zero() -> None:
    events = [
        {
            "type": "history_gap",
            "seq": 0,
            "data": {"reason": "stream_missing", "session_id": "sess-gap"},
        },
        {
            "type": "assistant_message",
            "seq": 42,
            "data": {"session_id": "sess-real", "content": "hello"},
        },
    ]

    assert _messages_cursor_anchor_event(events) == events[1]


def test_messages_cursor_anchor_event_requires_positive_seq_and_session_id() -> None:
    events = [
        {"type": "history_gap", "seq": 0, "data": {"session_id": "sess-gap"}},
        {"type": "assistant_message", "seq": 1, "data": {}},
    ]

    assert _messages_cursor_anchor_event(events) is None


def test_conversation_messages_older_cursor_skips_history_gap_seq_zero(
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
                    title="Cursor conversation",
                )
                await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    session_id="sess-old",
                    intaris_session_id="intaris-old",
                )
                await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-chat",
                    session_id="sess-current",
                    intaris_session_id="intaris-current",
                    previous_session_id="sess-old",
                )
                conversation.active_session_id = "sess-current"
                await session.commit()
                return conversation.conversation_id

        class GuardrailsStub:
            async def read_events(self, **kwargs: Any) -> SimpleNamespace:
                if kwargs["session_id"] == "intaris-old":
                    return SimpleNamespace(
                        events=[],
                        last_seq=0,
                        has_more=False,
                        missing_stream_fallback_used=True,
                    )
                assert kwargs["session_id"] == "intaris-current"
                return SimpleNamespace(
                    events=[
                        {
                            "type": "assistant_message",
                            "seq": 42,
                            "data": {"session_id": "sess-current", "content": "hello"},
                            "ts": "2026-06-11T12:00:00+00:00",
                        }
                    ],
                    last_seq=42,
                    has_more=True,
                    missing_stream_fallback_used=False,
                )

            async def get_last_seq(self, session_id: str) -> int:
                return 42 if session_id == "intaris-current" else 0

        conversation_id = asyncio.run(_seed())
        guardrails = GuardrailsStub()
        monkeypatch.setattr(app.state.providers.guardrails, "read_events", guardrails.read_events)
        monkeypatch.setattr(app.state.providers.guardrails, "get_last_seq", guardrails.get_last_seq)
        headers = _auth_headers(app, email="user@example.com")

        response = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            params={"anchor": "latest", "limit": 2},
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["has_more"] is True
        assert body["older_cursor"] is not None
        assert _decode_messages_cursor(body["older_cursor"]) == ("sess-current", 42)
