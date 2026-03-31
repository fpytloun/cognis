from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.events import EventBus, EventType
from cognis.core.session import SessionManager
from cognis.models.session import ConversationContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_user,
    get_latest_active_conversation_for_agent,
    set_session_intaris_session_id,
    update_conversation_active_session,
)


def _create_test_client(monkeypatch: object, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("COGNIS_DATA_DIR", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("COGNIS_HOST", "127.0.0.1")  # type: ignore[attr-defined]
    app = create_app()
    return TestClient(app)


def _auth_headers(app: object, *, email: str, role: str = "user") -> dict[str, str]:
    token = app.state.auth_provider.sign_access_token(email, email.split("@")[0].title(), role)  # type: ignore[attr-defined]
    return {"Authorization": f"Bearer {token}"}


def test_settings_update_rejects_unknown_key(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="admin@example.com",
                    name="Admin",
                    password_hash=app.state.password_hasher.hash("password123"),
                    role="admin",
                )
                await session.commit()

        asyncio.run(_seed())
        response = client.put(
            "/api/v1/settings/unknown.key",
            headers=_auth_headers(app, email="admin@example.com", role="admin"),
            json={"value": 1},
        )
        assert response.status_code == 400


def test_workflow_create_rejects_empty_steps(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        asyncio.run(_seed_user(app, "user@example.com"))

        response = client.post(
            "/api/v1/workflows",
            headers=_auth_headers(app, email="user@example.com"),
            json={"name": "Broken", "steps": []},
        )
        assert response.status_code == 400


def test_workflow_create_rejects_missing_input_reference(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        asyncio.run(_seed_user(app, "user@example.com"))

        response = client.post(
            "/api/v1/workflows",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "name": "Broken",
                "steps": [
                    {"name": "implement", "type": "run", "prompt": "Do work", "input": ["plan"]}
                ],
            },
        )
        assert response.status_code == 400


def test_agent_create_surfaces_personality_sync_failure(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        asyncio.run(_seed_user(app, "user@example.com"))

        async def _fail_bootstrap(_: object) -> None:
            raise RuntimeError("mnemory unavailable")

        app.state.providers.memory.bootstrap_agent = _fail_bootstrap

        response = client.post(
            "/api/v1/agents",
            headers=_auth_headers(app, email="user@example.com"),
            json={"agent_id": "agent-sync", "name": "Agent Sync"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["personality_synced"] is False


def test_agent_sync_endpoint_persists_sync_error(monkeypatch: object, tmp_path: Path) -> None:
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
                    agent_id="agent-sync",
                    owner_email="user@example.com",
                    name="Agent Sync",
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())

        async def _fail_bootstrap(_: object) -> None:
            raise RuntimeError("api_key=secret-value")

        app.state.providers.memory.bootstrap_agent = _fail_bootstrap
        response = client.post(
            "/api/v1/agents/agent-sync/sync-personality",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 502

        detail = client.get(
            "/api/v1/agents/agent-sync",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["personality_synced"] is False
        assert "secret-value" not in str(payload["personality_sync_error"])


def test_conversation_purge_reports_unsupported_intaris_cascade(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        conversation_id = asyncio.run(_seed_conversation(app, "user@example.com", "agent-1"))

        response = client.delete(
            f"/api/v1/conversations/{conversation_id}/purge",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json()["intaris_cascade"] is False


def test_conversation_purge_calls_intaris_delete_session_when_supported(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        conversation_id = asyncio.run(_seed_conversation(app, "user@example.com", "agent-1"))
        deleted: list[str] = []

        async def _delete_session(session_id: str) -> None:
            deleted.append(session_id)

        app.state.providers.guardrails.delete_session = _delete_session

        response = client.delete(
            f"/api/v1/conversations/{conversation_id}/purge",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200
        assert response.json()["intaris_cascade"] is True
        assert deleted


def test_latest_active_conversation_for_agent_prefers_recent_active(tmp_path: Path) -> None:
    async def _run() -> str | None:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session, email="user@example.com", name="User", password_hash="hash", role="user"
            )
            await create_agent(
                session, agent_id="agent-1", owner_email="user@example.com", name="Agent 1"
            )
            old_conversation = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                title="Old",
            )
            old_conversation.last_message_at = datetime.now(UTC) - timedelta(days=1)
            fresh_conversation = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                title="Fresh",
            )
            fresh_conversation.last_message_at = datetime.now(UTC)
            archived = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                title="Archived",
            )
            archived.status = "archived"
            archived.last_message_at = datetime.now(UTC) + timedelta(days=1)
            await session.commit()

        async with session_factory() as session:
            latest = await get_latest_active_conversation_for_agent(
                session, "user@example.com", "agent-1"
            )
        await engine.dispose()
        return latest.title if latest is not None else None

    assert asyncio.run(_run()) == "Fresh"


def test_session_manager_recover_stale_sessions_publishes_event(tmp_path: Path) -> None:
    async def _run() -> list[EventType]:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session, email="user@example.com", name="User", password_hash="hash", role="user"
            )
            await create_agent(
                session, agent_id="agent-1", owner_email="user@example.com", name="Agent 1"
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
            session_row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
            await session.commit()

        events: list[EventType] = []
        event_bus = EventBus()

        async def _capture(event: object) -> None:
            events.append(event.type)

        event_bus.subscribe_all(_capture)

        class _Cache:
            async def evict(self, _: str) -> None:
                return None

        manager = SessionManager(
            session_factory, providers=object(), session_cache=_Cache(), event_bus=event_bus
        )
        await manager.recover_stale_sessions(stale_after_seconds=300)
        await engine.dispose()
        return events

    assert EventType.SESSION_RECOVERED in asyncio.run(_run())


async def _seed_user(app: object, email: str) -> None:
    async with app.state.session_factory() as session:
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),
            role="user",
        )
        await session.commit()


async def _seed_conversation(app: object, email: str, agent_id: str) -> str:
    async with app.state.session_factory() as session:
        await create_user(
            session,
            email=email,
            name="User",
            password_hash=app.state.password_hasher.hash("password123"),
            role="user",
        )
        await create_agent(
            session,
            agent_id=agent_id,
            owner_email=email,
            name="Agent 1",
            status="active",
        )
        conversation = await create_conversation(
            session,
            user_email=email,
            agent_id=agent_id,
            context_type=ConversationContext(type="web").type,
            title="Conversation",
        )
        session_row = await create_session(
            session,
            conversation_id=conversation.conversation_id,
            user_email=email,
            agent_id=agent_id,
        )
        await set_session_intaris_session_id(
            session, session_row.session_id, f"intaris-{session_row.session_id}"
        )
        await update_conversation_active_session(
            session, conversation.conversation_id, session_row.session_id
        )
        await session.commit()
        return conversation.conversation_id
