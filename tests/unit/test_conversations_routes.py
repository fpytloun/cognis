from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_agent, create_conversation, create_user, get_conversation


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
