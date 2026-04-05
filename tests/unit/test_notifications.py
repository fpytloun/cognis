from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.store.queries import create_agent, create_conversation, create_session, create_user


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def test_reconcile_pending_marks_direct_chat_questions_orphaned(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
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
                    agent_id="agent-1",
                    owner_email="user@example.com",
                    name="Agent 1",
                    status="active",
                )
                conversation = await create_conversation(
                    session,
                    user_email="user@example.com",
                    agent_id="agent-1",
                    context_type="web",
                    title="Conversation",
                )
                session_row = await create_session(
                    session,
                    conversation_id=conversation.conversation_id,
                    user_email="user@example.com",
                    agent_id="agent-1",
                )
                await session.commit()
                await app.state.notification_service.create(
                    notification_type="step_question",
                    user_email="user@example.com",
                    conversation_id=conversation.conversation_id,
                    session_id=session_row.session_id,
                    notification_id="notif_restart_orphan",
                    payload={"question": "Need input"},
                )

        asyncio.run(_seed())

    with _create_test_client(monkeypatch, tmp_path) as restarted_client:
        app = restarted_client.app
        notification = asyncio.run(app.state.notification_service.get("notif_restart_orphan"))
        assert notification is not None
        assert notification.status == "resolved"
        assert notification.resolution == {
            "decision": "cancel",
            "reason": "controller_restart",
        }
        assert app.state.pause_waiter.get("notif_restart_orphan") is None
