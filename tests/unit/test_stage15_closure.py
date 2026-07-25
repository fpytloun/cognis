from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cognis.api.app import create_app
from cognis.core.agent_direct import AGENT_DIRECT_KIND, agent_direct_context_ref
from cognis.core.events import EventBus, EventType
from cognis.core.session import SessionManager
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_session,
    create_user,
    get_agent_direct_conversation,
    get_latest_active_conversation_for_agent,
    list_conversations,
    mark_conversation_agent_direct,
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


def test_app_startup_registers_required_state_services(monkeypatch: object, tmp_path: Path) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        state = client.app.state

        assert hasattr(state, "agent_registry")
        assert hasattr(state, "workflow_registry")
        assert hasattr(state, "turn_scheduler")
        assert hasattr(state, "command_dispatcher")
        assert hasattr(state, "task_queue")


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

        async def _fail_bootstrap(
            _: object,
            previous_content: str | None = None,
            allow_legacy_cleanup: bool = False,
        ) -> None:
            raise RuntimeError("api_key=secret-value")

        app.state.providers.memory.replace_bootstrap_identity = _fail_bootstrap
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


def test_agent_update_resyncs_personality_when_identity_changes(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        asyncio.run(_seed_user(app, "user@example.com"))

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent-sync",
                    owner_email="user@example.com",
                    name="Agent Sync",
                    system_prompt="Old prompt",
                    personality={"purpose": "old purpose"},
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        bootstrapped: list[tuple[AgentDefinition, str | None]] = []

        async def _capture_bootstrap(
            definition: AgentDefinition,
            previous_content: str | None = None,
            allow_legacy_cleanup: bool = False,
        ) -> None:
            bootstrapped.append((definition, previous_content))

        app.state.providers.memory.replace_bootstrap_identity = _capture_bootstrap

        response = client.put(
            "/api/v1/agents/agent-sync",
            headers=_auth_headers(app, email="user@example.com"),
            json={
                "system_prompt": "New prompt",
                "personality": {"purpose": "new purpose", "tone": "formal"},
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["personality_synced"] is True
        assert len(bootstrapped) == 1
        assert bootstrapped[0][0].system_prompt == "New prompt"
        assert bootstrapped[0][0].personality == {
            "purpose": "new purpose",
            "tone": "formal",
        }
        assert bootstrapped[0][1] == "Purpose: old purpose"


def test_agent_update_allows_clearing_nullable_identity_fields(
    monkeypatch: object, tmp_path: Path
) -> None:
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app
        asyncio.run(_seed_user(app, "user@example.com"))

        async def _seed() -> None:
            async with app.state.session_factory() as session:
                await create_agent(
                    session,
                    agent_id="agent-clear",
                    owner_email="user@example.com",
                    name="Agent Clear",
                    description="Has description",
                    system_prompt="Has prompt",
                    personality={"purpose": "helper"},
                    status="active",
                )
                await session.commit()

        asyncio.run(_seed())
        bootstrapped: list[tuple[AgentDefinition, str | None]] = []

        async def _capture_bootstrap(
            definition: AgentDefinition,
            previous_content: str | None = None,
            allow_legacy_cleanup: bool = False,
        ) -> None:
            bootstrapped.append((definition, previous_content))

        app.state.providers.memory.replace_bootstrap_identity = _capture_bootstrap

        response = client.put(
            "/api/v1/agents/agent-clear",
            headers=_auth_headers(app, email="user@example.com"),
            json={"description": None, "system_prompt": None, "personality": None},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["description"] is None
        assert payload["system_prompt"] is None
        assert payload["personality"] is None
        assert payload["personality_synced"] is True
        assert len(bootstrapped) == 1
        assert bootstrapped[0][0].system_prompt is None
        assert bootstrapped[0][0].personality is None
        assert bootstrapped[0][1] == "Purpose: helper"


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


def test_agent_direct_conversations_are_hidden_from_default_history(tmp_path: Path) -> None:
    async def _run() -> tuple[list[str], list[str], str | None]:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session, email="user@example.com", name="User", password_hash="hash", role="user"
            )
            await create_agent(
                session,
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent 1",
                status="active",
            )
            normal = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                title="Normal",
            )
            await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                context_data={"kind": "topic"},
                title="Topic",
            )
            direct = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                context_ref=agent_direct_context_ref("user@example.com", "agent-1"),
                context_data={"kind": AGENT_DIRECT_KIND},
                title=None,
                title_source="agent_direct",
            )
            await session.commit()

        async with session_factory() as session:
            default_rows = await list_conversations(
                session,
                "user@example.com",
                context_type="web",
                include_agent_direct=False,
            )
            all_rows = await list_conversations(
                session,
                "user@example.com",
                context_type="web",
                include_agent_direct=True,
            )
            found_direct = await get_agent_direct_conversation(
                session,
                "user@example.com",
                "agent-1",
            )

        await engine.dispose()
        assert normal.conversation_id != direct.conversation_id
        return (
            [row.conversation_id for row in default_rows],
            [row.conversation_id for row in all_rows],
            found_direct.conversation_id if found_direct else None,
        )

    default_ids, all_ids, direct_id = asyncio.run(_run())
    assert len(default_ids) == 2
    assert direct_id not in default_ids
    assert direct_id in all_ids


def test_agent_direct_lookup_canonicalizes_legacy_direct_context(tmp_path: Path) -> None:
    async def _run() -> tuple[str | None, str | None, list[str], str | None]:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session, email="user@example.com", name="User", password_hash="hash", role="user"
            )
            await create_agent(
                session,
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent 1",
                status="active",
            )
            await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                title="Normal",
            )
            legacy = await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                context_data={"kind": AGENT_DIRECT_KIND},
                title_source="agent_direct",
            )
            await create_session(
                session,
                conversation_id=legacy.conversation_id,
                user_email="user@example.com",
                agent_id="agent-1",
                session_id="sess-legacy",
            )
            await session.commit()

        async with session_factory() as session:
            found = await get_agent_direct_conversation(session, "user@example.com", "agent-1")
            assert found is not None
            await mark_conversation_agent_direct(
                session,
                found.conversation_id,
                user_email="user@example.com",
                agent_id="agent-1",
            )
            await session.commit()
            canonical = await get_agent_direct_conversation(session, "user@example.com", "agent-1")
            default_rows = await list_conversations(
                session,
                "user@example.com",
                context_type="web",
                include_agent_direct=False,
            )
            latest = await get_latest_active_conversation_for_agent(
                session,
                "user@example.com",
                "agent-1",
                context_type="web",
            )

        await engine.dispose()
        assert found.conversation_id == legacy.conversation_id
        return (
            canonical.conversation_id if canonical else None,
            canonical.context_ref if canonical else None,
            [row.conversation_id for row in default_rows],
            latest.conversation_id if latest else None,
        )

    conversation_id, context_ref, default_ids, latest_id = asyncio.run(_run())
    assert conversation_id is not None
    assert context_ref == agent_direct_context_ref("user@example.com", "agent-1")
    assert conversation_id not in default_ids
    assert latest_id != conversation_id


def test_session_manager_creates_agent_direct_with_stable_agent_title(tmp_path: Path) -> None:
    async def _run() -> tuple[str | None, str]:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session, email="user@example.com", name="User", password_hash="hash", role="user"
            )
            await create_agent(
                session,
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent 1",
                display_name="Miroslav",
                status="active",
            )
            await session.commit()

        manager = SessionManager(session_factory, providers=object(), session_cache=object())
        conversation = await manager.get_or_create_agent_direct_conversation(
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await engine.dispose()
        return conversation.title, conversation.title_source

    assert asyncio.run(_run()) == ("Miroslav", "agent_direct")


def test_session_manager_repairs_intaris_titled_agent_direct_chat(tmp_path: Path) -> None:
    async def _run() -> tuple[str | None, str, dict[str, object] | None]:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            await create_user(
                session, email="user@example.com", name="User", password_hash="hash", role="user"
            )
            await create_agent(
                session,
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent 1",
                display_name="Miroslav",
                status="active",
            )
            await create_conversation(
                session,
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
                context_ref=agent_direct_context_ref("user@example.com", "agent-1"),
                context_data={
                    "kind": AGENT_DIRECT_KIND,
                    "intaris_latest_title": "Lawn stain cause investigation",
                },
                title="Lawn stain cause investigation",
                title_source="intaris",
            )
            await session.commit()

        manager = SessionManager(session_factory, providers=object(), session_cache=object())
        conversation = await manager.get_or_create_agent_direct_conversation(
            user_email="user@example.com",
            agent_id="agent-1",
        )
        await engine.dispose()
        return conversation.title, conversation.title_source, conversation.context.platform_data

    title, title_source, platform_data = asyncio.run(_run())
    assert title == "Miroslav"
    assert title_source == "agent_direct"
    assert platform_data == {
        "kind": AGENT_DIRECT_KIND,
        "intaris_latest_title": "Lawn stain cause investigation",
    }


def test_session_manager_recover_stale_sessions_publishes_event(tmp_path: Path) -> None:
    async def _run() -> list[object]:
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

        events: list[object] = []
        event_bus = EventBus()

        async def _capture(event: object) -> None:
            events.append(event)

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

    events = asyncio.run(_run())
    recovered_event = next(event for event in events if event.type == EventType.SESSION_RECOVERED)
    assert recovered_event.data["reason"] == "controller_restart"
    assert recovered_event.data["title"] == "Controller restarted"
    assert recovered_event.data["message"] == (
        "The controller restarted while this session was active. "
        "Saved work is preserved; resume the session if needed."
    )


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
