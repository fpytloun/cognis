from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cognis.core.session import SessionManager
from cognis.models.session import ConversationContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, Session, User


class _Guardrails:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict | None = None,
        details: dict | None = None,
    ) -> None:
        del policy, details
        if self.fail:
            raise RuntimeError("intaris unavailable")
        self.calls.append((session_id, agent_id, parent_session_id))


class _Providers:
    def __init__(self, fail: bool = False) -> None:
        self.guardrails = _Guardrails(fail=fail)
        self.memory = object()


class _Cache:
    def __init__(self) -> None:
        self.evicted: list[str] = []

    async def evict(self, session_id: str) -> bool:
        self.evicted.append(session_id)
        return True


async def _session_factory(tmp_path) -> object:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cognis.db'}")
    session_factory = create_session_factory(engine)
    from cognis.bootstrap import run_schema_bootstrap

    await run_schema_bootstrap(engine)
    async with session_factory() as session:
        session.add(User(email="user@example.com", name="User", password_hash="x", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="user@example.com",
                name="Agent One",
                description="Helpful assistant",
            )
        )
        await session.commit()
    return engine, session_factory


@pytest.mark.asyncio
async def test_session_manager_creates_conversation_and_root_session_atomically(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    conversation, root_session = await manager.create_conversation_with_root_session(
        user_email="user@example.com",
        agent_id="agent-1",
        context=ConversationContext(type="web"),
        title="Test conversation",
    )

    assert conversation.root_session_id == root_session.session_id
    assert root_session.intaris_session_id == root_session.session_id

    async with session_factory() as session:
        stored_conversation = await session.get(Conversation, conversation.conversation_id)
        stored_session = await session.get(Session, root_session.session_id)
        assert stored_conversation is not None
        assert stored_conversation.root_session_id == root_session.session_id
        assert stored_session is not None
        assert stored_session.intaris_session_id == root_session.session_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_session_manager_rolls_back_when_intaris_creation_fails(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(fail=True), _Cache())

    with pytest.raises(RuntimeError, match="intaris unavailable"):
        await manager.create_conversation_with_root_session(
            user_email="user@example.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
            title="Broken conversation",
        )

    async with session_factory() as session:
        assert (await session.execute(Session.__table__.select())).all() == []
        assert (await session.execute(Conversation.__table__.select())).all() == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_session_manager_recovery_uses_updated_at_not_started_at(tmp_path) -> None:
    engine, session_factory = await _session_factory(tmp_path)
    manager = SessionManager(session_factory, _Providers(), _Cache())

    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            Conversation(
                conversation_id="conv-1",
                user_email="user@example.com",
                agent_id="agent-1",
                context_type="web",
            )
        )
        session.add_all(
            [
                Session(
                    session_id="fresh-session",
                    conversation_id="conv-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=2),
                    updated_at=now,
                ),
                Session(
                    session_id="stale-parent",
                    conversation_id="conv-1",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=2),
                    updated_at=now - timedelta(minutes=20),
                ),
                Session(
                    session_id="stale-child",
                    conversation_id="conv-1",
                    parent_session_id="stale-parent",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=20),
                ),
                Session(
                    session_id="stale-grandchild",
                    conversation_id="conv-1",
                    parent_session_id="stale-child",
                    user_email="user@example.com",
                    agent_id="agent-1",
                    status="active",
                    started_at=now - timedelta(hours=1),
                    updated_at=now - timedelta(minutes=20),
                ),
            ]
        )
        await session.commit()

    recovered_ids = await manager.recover_stale_sessions(stale_after_seconds=300)

    assert set(recovered_ids) == {"stale-parent", "stale-child", "stale-grandchild"}
    async with session_factory() as session:
        fresh = await session.get(Session, "fresh-session")
        stale_parent = await session.get(Session, "stale-parent")
        stale_child = await session.get(Session, "stale-child")
        stale_grandchild = await session.get(Session, "stale-grandchild")
        assert fresh is not None and fresh.status == "active"
        assert stale_parent is not None and stale_parent.status == "idle"
        assert stale_parent.idle_since is not None
        assert stale_child is not None and stale_child.status == "failed"
        assert stale_grandchild is not None and stale_grandchild.status == "failed"

    await engine.dispose()
