from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text

from cognis.bootstrap import run_schema_bootstrap
from cognis.core.agent_loop import CHAT_POLICY, AgentLoop, StepContext
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationContext, ConversationModel
from cognis.models.workflow import StepDefinition
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Conversation, User
from cognis.store.queries import (
    copy_session_todos,
    create_session,
    list_conversation_todos,
    list_session_todos,
    replace_conversation_todos,
    replace_session_todos,
)


class _PostgresDialect:
    name = "postgresql"


class _PostgresBind:
    dialect = _PostgresDialect()


class _RecordingSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self.added: list[Any] = []
        self.flushed = False

    def get_bind(self) -> _PostgresBind:
        return _PostgresBind()

    async def execute(self, statement: object, params: dict[str, Any] | None = None) -> None:
        self.executed.append((str(statement), params))

    def add_all(self, rows: object) -> None:
        self.added.extend(list(rows))  # type: ignore[arg-type]

    async def flush(self) -> None:
        self.flushed = True


async def _seed_conversation(session: object, conversation_id: str = "conv_1") -> None:
    session.add(User(email="user@test.com", name="User", role="admin"))  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="Agent"))  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    session.add(  # type: ignore[attr-defined]
        Conversation(
            conversation_id=conversation_id,
            user_email="user@test.com",
            agent_id="agent-1",
            context_type="web",
        )
    )
    await session.flush()  # type: ignore[attr-defined]


def _agent_loop_with_session_factory(factory: object) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.session_manager = SimpleNamespace(session_factory=factory)
    loop._session_factory = factory
    return loop


def _step_ctx(
    *,
    session_id: str,
    conversation_id: str = "conv_1",
    parent_session_id: str | None = None,
    todos: list[dict[str, Any]] | None = None,
) -> StepContext:
    return StepContext(
        step_definition=StepDefinition(name="direct", type="run", prompt=""),
        session=SimpleNamespace(
            session_id=session_id,
            conversation_id=conversation_id,
            parent_session_id=parent_session_id,
            user_email="user@test.com",
            agent_id="agent-1",
        ),
        conversation=ConversationModel(
            conversation_id=conversation_id,
            user_email="user@test.com",
            agent_id="agent-1",
            context=ConversationContext(type="web"),
        ),
        agent=AgentDefinition(agent_id="agent-1", owner_email="user@test.com", name="Agent"),
        policy=CHAT_POLICY,
        todos=todos or [],
    )


@pytest.mark.asyncio
async def test_conversation_todos_postgres_replacement_takes_advisory_lock() -> None:
    session = _RecordingSession()

    persisted = await replace_conversation_todos(
        session,  # type: ignore[arg-type]
        "conv_1",
        [{"content": "new", "status": "in_progress"}],
    )

    assert persisted == [{"content": "new", "status": "in_progress"}]
    assert len(session.executed) == 2
    lock_statement, lock_params = session.executed[0]
    delete_statement, _ = session.executed[1]
    assert "pg_advisory_xact_lock" in lock_statement
    assert lock_params == {"key": "todo-replace:conversation:conv_1"}
    assert delete_statement.startswith("DELETE FROM conversation_todos")
    assert len(session.added) == 1
    assert session.flushed is True


@pytest.mark.asyncio
async def test_conversation_todos_replace_and_clear_atomically(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/conversation-todos.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await replace_conversation_todos(
                session,
                "conv_1",
                [
                    {"content": "old", "status": "pending"},
                    {"content": "keep", "status": "in_progress", "priority": "high"},
                ],
            )
            await replace_conversation_todos(
                session,
                "conv_1",
                [{"content": "new", "status": "in_progress"}],
            )
            await session.commit()

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "new", "status": "in_progress"}
            ]
            await replace_conversation_todos(session, "conv_1", [])
            await session.commit()

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_todos_preserve_parallel_in_progress_items(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/parallel-conversation-todos.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    todos = [
        {"content": "Implement backend", "status": "in_progress"},
        {"content": "Validate frontend contract", "status": "in_progress"},
    ]
    try:
        async with factory() as session:
            await _seed_conversation(session)
            assert await replace_conversation_todos(session, "conv_1", todos) == todos
            await session.commit()

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == todos
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_todos_replace_and_clear_atomically(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/session-todos.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_1",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await replace_session_todos(
                session,
                "sess_1",
                [
                    {"content": "old", "status": "pending"},
                    {"content": "keep", "status": "in_progress", "priority": "high"},
                ],
            )
            await replace_session_todos(
                session,
                "sess_1",
                [{"content": "new", "status": "in_progress"}],
            )
            await session.commit()

        async with factory() as session:
            assert await list_session_todos(session, "sess_1") == [
                {"content": "new", "status": "in_progress"}
            ]
            await replace_session_todos(session, "sess_1", [])
            await session.commit()

        async with factory() as session:
            assert await list_session_todos(session, "sess_1") == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_does_not_load_parent_conversation_todos(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/child-todos-load.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_parent",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await create_session(
                session,
                session_id="sess_child",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                parent_session_id="sess_parent",
                delegation_mode="sync",
            )
            await replace_conversation_todos(
                session,
                "conv_1",
                [{"content": "parent done", "status": "completed"}],
            )
            await session.commit()

        loop = _agent_loop_with_session_factory(factory)
        ctx = _step_ctx(session_id="sess_child", parent_session_id="sess_parent")

        await loop._load_session_todos(ctx)

        assert ctx.todos == []
        async with factory() as session:
            assert await list_session_todos(session, "sess_child") == []
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "parent done", "status": "completed"}
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_loads_own_session_todos(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/child-own-todos-load.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_parent",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await create_session(
                session,
                session_id="sess_child",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                parent_session_id="sess_parent",
                delegation_mode="sync",
            )
            await replace_conversation_todos(
                session,
                "conv_1",
                [{"content": "parent done", "status": "completed"}],
            )
            await replace_session_todos(
                session,
                "sess_child",
                [{"content": "child active", "status": "in_progress"}],
            )
            await session.commit()

        loop = _agent_loop_with_session_factory(factory)
        ctx = _step_ctx(session_id="sess_child", parent_session_id="sess_parent")

        await loop._load_session_todos(ctx)

        assert ctx.todos == [{"content": "child active", "status": "in_progress"}]
        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "parent done", "status": "completed"}
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_persists_only_session_todos(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/child-todos-persist.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_parent",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await create_session(
                session,
                session_id="sess_child",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                parent_session_id="sess_parent",
                delegation_mode="sync",
            )
            await replace_conversation_todos(
                session,
                "conv_1",
                [{"content": "parent todo", "status": "in_progress"}],
            )
            await session.commit()

        loop = _agent_loop_with_session_factory(factory)
        ctx = _step_ctx(
            session_id="sess_child",
            parent_session_id="sess_parent",
            todos=[{"content": "child todo", "status": "completed"}],
        )

        await loop._persist_todos(ctx)

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "parent todo", "status": "in_progress"}
            ]
            assert await list_session_todos(session, "sess_child") == [
                {"content": "child todo", "status": "completed"}
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_child_session_does_not_persist_execution_paths_to_conversation(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/child-paths.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_parent",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await create_session(
                session,
                session_id="sess_child",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                parent_session_id="sess_parent",
                delegation_mode="sync",
            )
            await session.commit()

        loop = _agent_loop_with_session_factory(factory)
        ctx = _step_ctx(session_id="sess_child", parent_session_id="sess_parent")

        await loop._persist_execution_paths(
            ctx,
            workspace_root="/tmp/child-root",
            working_directory="/tmp/child-root/worktree",
        )

        async with factory() as session:
            row = (
                await session.execute(
                    select(Conversation).where(Conversation.conversation_id == "conv_1")
                )
            ).scalar_one()
            assert row.context_data is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_todos_copy_preserves_order_for_compaction(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/session-todos-copy.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_old",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await create_session(
                session,
                session_id="sess_new",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                previous_session_id="sess_old",
            )
            await replace_session_todos(
                session,
                "sess_old",
                [
                    {"content": "first", "status": "completed"},
                    {"content": "second", "status": "in_progress"},
                ],
            )
            copied = await copy_session_todos(
                session,
                source_session_id="sess_old",
                target_session_id="sess_new",
            )
            await session.commit()

        assert copied == [
            {"content": "first", "status": "completed"},
            {"content": "second", "status": "in_progress"},
        ]
        async with factory() as session:
            assert await list_session_todos(session, "sess_new") == copied
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retry_seeded_todos_sync_to_session_state_before_compaction(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/session-todos-retry.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_retry",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await replace_session_todos(
                session,
                "sess_retry",
                [{"content": "stale persisted", "status": "pending"}],
            )
            await session.commit()

        loop = AgentLoop.__new__(AgentLoop)
        loop.session_manager = SimpleNamespace(session_factory=factory)
        ctx = SimpleNamespace(
            session=SimpleNamespace(session_id="sess_retry", conversation_id="conv_1"),
            step_run_id=None,
            todos=[{"content": "retry seeded", "status": "in_progress"}],
        )
        await loop._load_session_todos(ctx)

        async with factory() as session:
            assert await list_session_todos(session, "sess_retry") == [
                {"content": "retry seeded", "status": "in_progress"}
            ]
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "retry seeded", "status": "in_progress"}
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_backing_session_loads_conversation_todos(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/conversation-todos-load.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_new",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await replace_conversation_todos(
                session,
                "conv_1",
                [{"content": "carry across user turn", "status": "in_progress"}],
            )
            await session.commit()

        loop = AgentLoop.__new__(AgentLoop)
        loop.session_manager = SimpleNamespace(session_factory=factory)
        ctx = SimpleNamespace(
            session=SimpleNamespace(session_id="sess_new", conversation_id="conv_1"),
            step_run_id=None,
            todos=[],
        )
        await loop._load_session_todos(ctx)

        assert ctx.todos == [{"content": "carry across user turn", "status": "in_progress"}]
        async with factory() as session:
            assert await list_session_todos(session, "sess_new") == ctx.todos
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_backfills_conversation_todos_from_existing_session_todos(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/conversation-todos-backfill.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_existing",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await session.execute(
                text(
                    "UPDATE conversations SET active_session_id = 'sess_existing' "
                    "WHERE conversation_id = 'conv_1'"
                )
            )
            await replace_session_todos(
                session,
                "sess_existing",
                [{"content": "pre-upgrade todo", "status": "in_progress"}],
            )
            await session.execute(text("DELETE FROM conversation_todos"))
            await session.commit()

        await run_schema_bootstrap(engine)

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "pre-upgrade todo", "status": "in_progress"}
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_backfills_latest_active_session_when_active_session_id_is_missing(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/conversation-todos-null-active.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_old",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                status="idle",
            )
            await create_session(
                session,
                session_id="sess_latest",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
                status="idle",
            )
            await session.execute(
                text(
                    "UPDATE conversations SET active_session_id = NULL "
                    "WHERE conversation_id = 'conv_1'"
                )
            )
            await session.execute(
                text(
                    "UPDATE sessions SET started_at = '2026-01-01 00:00:00', "
                    "updated_at = '2026-01-01 00:00:00' WHERE session_id = 'sess_old'"
                )
            )
            await session.execute(
                text(
                    "UPDATE sessions SET started_at = '2026-01-02 00:00:00', "
                    "updated_at = '2026-01-02 00:00:00' WHERE session_id = 'sess_latest'"
                )
            )
            await replace_session_todos(
                session,
                "sess_old",
                [{"content": "old todo", "status": "pending"}],
            )
            await replace_session_todos(
                session,
                "sess_latest",
                [{"content": "latest todo", "status": "in_progress"}],
            )
            await session.execute(text("DELETE FROM conversation_todos"))
            await session.commit()

        await run_schema_bootstrap(engine)

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == [
                {"content": "latest todo", "status": "in_progress"}
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bootstrap_backfill_does_not_resurrect_old_session_todos_when_active_is_empty(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/conversation-todos-no-resurrect.db")
    await run_schema_bootstrap(engine)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await _seed_conversation(session)
            await create_session(
                session,
                session_id="sess_old",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await create_session(
                session,
                session_id="sess_active",
                conversation_id="conv_1",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await session.execute(
                text(
                    "UPDATE conversations SET active_session_id = 'sess_active' "
                    "WHERE conversation_id = 'conv_1'"
                )
            )
            await replace_session_todos(
                session,
                "sess_old",
                [{"content": "cleared stale todo", "status": "in_progress"}],
            )
            await session.execute(text("DELETE FROM conversation_todos"))
            await session.commit()

        await run_schema_bootstrap(engine)

        async with factory() as session:
            assert await list_conversation_todos(session, "conv_1") == []
    finally:
        await engine.dispose()
