from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.sql import visitors
from sqlalchemy.sql.selectable import Subquery

from cognis.store.models import Agent, Base, Conversation, Session, User
from cognis.store.queries import (
    list_active_delegation_sessions,
    list_delegation_sessions_in_active_scope,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_scope_join_projects_only_identity_and_scope(scoped: bool) -> None:
    result = Mock()
    result.scalars.return_value.all.return_value = []
    db = Mock(execute=AsyncMock(return_value=result))
    if scoped:
        await list_delegation_sessions_in_active_scope(
            db, user_email="owner@local.test", controller_conversation_id="conversation"
        )
    else:
        await list_active_delegation_sessions(db, user_email="owner@local.test")
    statement = db.execute.call_args.args[0]
    scopes = {node for node in visitors.iterate(statement) if isinstance(node, Subquery)}
    assert len(scopes) == 1
    scope = scopes.pop()
    assert set(scope.c.keys()) == {"session_id", "activity_scope_id"}
    # Preserve full child Session return values; narrow only the scope lookup.
    assert statement.column_descriptions[0]["entity"] is Session
    assert statement._generate_cache_key() is not None
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        assert statement.compile(dialect=dialect) is not None


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request):
    url = "sqlite+aiosqlite:///:memory:"
    if request.param == "postgres":
        url = os.getenv("COGNIS_TEST_DELEGATION_QUERY_POSTGRES_URL")
        if not url:
            pytest.skip("dedicated PostgreSQL fixture not configured")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scope_queries_preserve_ownership_status_order_limits_and_rotation(db) -> None:
    db.add_all([User(email="a@local.test"), User(email="b@local.test")])
    await db.flush()
    db.add_all(
        [
            Agent(agent_id="agent-a", owner_email="a@local.test", name="A"),
            Agent(agent_id="agent-b", owner_email="b@local.test", name="B"),
        ]
    )
    await db.flush()
    conversations = {}
    for name, owner in (("a", "a"), ("b", "b"), ("c", "a"), ("no-active", "a")):
        conversations[name] = Conversation(
            conversation_id=name,
            user_email=f"{owner}@local.test",
            agent_id=f"agent-{owner}",
            context_type="web",
        )
        db.add(conversations[name])
    await db.flush()
    for name, owner in (("a", "a"), ("b", "b"), ("c", "a")):
        db.add(
            Session(
                session_id=f"root-{name}",
                conversation_id=name,
                user_email=f"{owner}@local.test",
                agent_id=f"agent-{owner}",
                activity_scope_id=f"scope-{name}",
            )
        )
    await db.flush()
    for name in ("a", "b", "c"):
        conversations[name].active_session_id = f"root-{name}"
    baseline = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        ("older", "a", "a", "scope-a", None, "active"),
        ("newer", "a", "a", "scope-a", "delegate", "active"),
        ("done", "a", "a", "scope-a", "delegate", "completed"),
        ("stale", "a", "a", "previous-scope", "delegate", "active"),
        ("hook", "a", "a", "scope-a", "hook", "active"),
        ("other-user", "b", "b", "scope-b", "delegate", "active"),
        ("other-conversation", "c", "a", "scope-c", "delegate", "active"),
        ("no-active-child", "no-active", "a", "scope-a", "delegate", "active"),
    ]
    for index, (name, conversation, owner, scope, mode, status) in enumerate(rows):
        db.add(
            Session(
                session_id=name,
                conversation_id=conversation,
                user_email=f"{owner}@local.test",
                agent_id=f"agent-{owner}",
                parent_session_id=f"root-{conversation}"
                if conversation != "no-active"
                else "root-a",
                activity_scope_id=scope,
                delegation_mode=mode,
                status=status,
                updated_at=baseline + timedelta(seconds=index),
                started_at=baseline + timedelta(seconds=index),
                delegation_metadata={"fixture": [name]},
            )
        )
    await db.flush()

    active = await list_active_delegation_sessions(db, user_email="a@local.test")
    assert [row.session_id for row in active] == ["other-conversation", "newer", "older"]
    assert active[0].delegation_metadata == {"fixture": ["other-conversation"]}
    active = await list_active_delegation_sessions(
        db, user_email="a@local.test", controller_conversation_id="a"
    )
    assert [row.session_id for row in active] == ["newer", "older"]
    scoped = await list_delegation_sessions_in_active_scope(
        db, user_email="a@local.test", controller_conversation_id="a"
    )
    assert [row.session_id for row in scoped] == ["done", "newer", "older"]
    for limit in (0, -1, 1):
        scoped = await list_delegation_sessions_in_active_scope(
            db, user_email="a@local.test", controller_conversation_id="a", limit=limit
        )
        assert [row.session_id for row in scoped] == ["done"]
    assert not await list_active_delegation_sessions(
        db, user_email="a@local.test", controller_conversation_id="b"
    )
    # A scope rotation must immediately hide previous-scope work, without caching.
    root = await db.get(Session, "root-a")
    root.activity_scope_id = "rotated-scope"
    await db.flush()
    assert not await list_delegation_sessions_in_active_scope(
        db, user_email="a@local.test", controller_conversation_id="a"
    )
