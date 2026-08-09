from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from cognis.core.executor_pin_lifecycle import ensure_active_executor_pin
from cognis.core.executor_pool import ExecutorAvailability, ExecutorPool, ResolvedExecutorTarget
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    Base,
    Conversation,
    ExecutorPinNoticeOutboxRow,
    ExecutorPinTransitionRow,
    Task,
    User,
)
from cognis.store.queries import (
    canonicalize_executor_pin_source,
    cas_executor_failover,
    clear_executor_unavailable,
    initialize_task_and_conversation_active_executor,
    mark_executor_unavailable,
    switch_task_and_conversation_active_executor,
)


class _ReadyConnection:
    connected = True


class _ReadyProvider:
    def get_connection(self, executor_id: str) -> _ReadyConnection | None:
        return _ReadyConnection() if executor_id == "replacement-sqlite" else None


def _sqlite_target(executor_id: str) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=True,
        selection_source="selector",
        description=None,
        state=ExecutorAvailability.USABLE,
        row=object(),
    )


@pytest.mark.asyncio
async def test_legacy_task_pin_source_is_canonicalized_with_exact_projection(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-source-canonical.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="source@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="source-agent", owner_email="source@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="source-conv",
                user_email="source@example.com",
                agent_id="source-agent",
                context_type="chat",
                active_executor_id="missing",
                active_executor_source="initial",
                active_executor_generation=4,
            )
        )
        session.add(
            Task(
                task_id="source-task",
                title="T",
                created_by="source@example.com",
                agent_id="source-agent",
                active_executor_id="missing",
                active_executor_source="initial",
                active_executor_generation=4,
            )
        )
        await session.commit()

    async with factory() as session:
        assert await canonicalize_executor_pin_source(
            session,
            conversation_id="source-conv",
            task_id="source-task",
            expected_executor_id="missing",
            expected_generation=4,
            source="selector_primary",
        )
        await session.commit()

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "source-task"))
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "source-conv")
        )
        assert task is not None and conversation is not None
        assert task.active_executor_source == "selector_primary"
        assert conversation.active_executor_source == "selector_primary"
        assert task.active_executor_generation == conversation.active_executor_generation == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_authority_projects_generation_and_pin_on_sqlite(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="u@example.com",
                agent_id="a",
                context_type="chat",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
            )
        )
        await session.commit()

    async with factory() as session:
        assert await initialize_task_and_conversation_active_executor(
            session,
            task_id="task",
            conversation_id="conv",
            active_executor_id="primary",
        )
        await session.commit()

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "conv")
        )
        assert task is not None and conversation is not None
        assert task.active_executor_id == conversation.active_executor_id == "primary"
        assert task.active_executor_generation == conversation.active_executor_generation == 1
        assert not await initialize_task_and_conversation_active_executor(
            session,
            task_id="task",
            conversation_id="conv",
            active_executor_id="other",
        )
        await session.rollback()

        won, generation, _ = await cas_executor_failover(
            session,
            task_id="task",
            conversation_id="conv",
            expected_executor_id="primary",
            new_executor_id="replacement",
            expected_generation=1,
            reason="persisted grace elapsed",
        )
        assert won and generation == 2
        await session.commit()

    async with factory() as session:
        lost, _, _ = await cas_executor_failover(
            session,
            task_id="task",
            conversation_id="conv",
            expected_executor_id="primary",
            new_executor_id="third",
            expected_generation=1,
            reason="racing admission",
        )
        assert not lost
        await session.rollback()


@pytest.mark.asyncio
async def test_existing_task_pin_projects_all_lifecycle_fields_exactly(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-projection.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    assigned_at = datetime.now(UTC) - timedelta(minutes=1)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="u@example.com",
                agent_id="a",
                context_type="chat",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
                active_executor_id="additional",
                active_executor_assigned_at=assigned_at,
                active_executor_expires_at=expires_at,
                active_executor_source="additional_explicit",
                active_executor_generation=4,
            )
        )
        await session.commit()

    async with factory() as session:
        assert not await initialize_task_and_conversation_active_executor(
            session,
            task_id="task",
            conversation_id="conv",
            active_executor_id="other",
        )
        await session.commit()

    async with factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "conv")
        )
        assert conversation is not None
        assert conversation.active_executor_id == "additional"
        assert conversation.active_executor_assigned_at is not None
        assert conversation.active_executor_expires_at is not None
        assert conversation.active_executor_assigned_at.replace(tzinfo=UTC) == assigned_at
        assert conversation.active_executor_expires_at.replace(tzinfo=UTC) == expires_at
        assert conversation.active_executor_source == "additional_explicit"
        assert conversation.active_executor_generation == 4

    unavailable_since = datetime.now(UTC)
    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "conv")
        )
        assert task is not None and conversation is not None
        task.active_executor_id = "switched"
        task.active_executor_generation = 5
        task.active_executor_unavailable_since = unavailable_since
        conversation.active_executor_id = "switched"
        conversation.active_executor_generation = 5
        conversation.active_executor_unavailable_since = unavailable_since
        await session.commit()

    async with factory() as session:
        assert not await clear_executor_unavailable(
            session,
            conversation_id="conv",
            task_id="task",
            expected_executor_id="additional",
            expected_generation=4,
        )
        await session.commit()
    async with factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "conv")
        )
        assert conversation is not None
        assert conversation.active_executor_id == "switched"
        assert conversation.active_executor_generation == 5
        assert conversation.active_executor_unavailable_since is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_conversation_cannot_mutate_task_or_create_failover_ledger(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-missing-conversation.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
            )
        )
        await session.commit()

    async with factory() as session:
        assert not await initialize_task_and_conversation_active_executor(
            session,
            task_id="task",
            conversation_id="missing",
            active_executor_id="primary",
        )
        await session.commit()

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        assert task is not None
        assert task.active_executor_id is None
        assert task.active_executor_generation == 0
        task.active_executor_id = "primary"
        task.active_executor_generation = 1
        task.active_executor_source = "selector"
        await session.commit()

    async with factory() as session:
        won, generation, notice_id = await cas_executor_failover(
            session,
            task_id="task",
            conversation_id="missing",
            expected_executor_id="primary",
            new_executor_id="replacement",
            expected_generation=1,
            reason="persisted grace elapsed",
        )
        assert not won
        assert generation == 1
        assert notice_id is None
        await session.commit()

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        transitions = await session.scalar(
            select(func.count()).select_from(ExecutorPinTransitionRow)
        )
        outbox = await session.scalar(select(func.count()).select_from(ExecutorPinNoticeOutboxRow))
        assert task is not None
        assert task.active_executor_id == "primary"
        assert task.active_executor_generation == 1
        assert transitions == 0
        assert outbox == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_and_switch_keep_task_and_conversation_in_sync(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-mark-switch.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="u@example.com",
                agent_id="a",
                context_type="chat",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        await session.commit()

    async with factory() as session:
        marked, observed = await mark_executor_unavailable(
            session,
            conversation_id="conv",
            task_id="task",
            expected_executor_id="old",
            expected_generation=1,
            observed_at=datetime.now(UTC),
        )
        assert marked and observed is not None
        await session.commit()

    async with factory() as session:
        switched, generation = await switch_task_and_conversation_active_executor(
            session,
            task_id="task",
            conversation_id="conv",
            active_executor_id="new",
        )
        assert switched and generation == 2
        await session.commit()

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "conv")
        )
        assert task is not None and conversation is not None
        assert task.active_executor_id == conversation.active_executor_id == "new"
        assert task.active_executor_generation == conversation.active_executor_generation == 2
        assert task.active_executor_unavailable_since is None
        assert conversation.active_executor_unavailable_since is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_projection_failure_rolls_back_authoritative_task(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-mark-rollback.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="u@example.com",
                agent_id="a",
                context_type="chat",
                active_executor_id="different",
                active_executor_generation=2,
            )
        )
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        await session.commit()

    async with factory() as session:
        marked, _ = await mark_executor_unavailable(
            session,
            conversation_id="conv",
            task_id="task",
            expected_executor_id="old",
            expected_generation=1,
            observed_at=datetime.now(UTC),
        )
        assert not marked
        await session.commit()

    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        assert task is not None
        assert task.active_executor_unavailable_since is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_and_switch_are_raced_with_conversation_first_barrier(tmp_path: object) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-mark-race.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="u@example.com",
                agent_id="a",
                context_type="chat",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
                active_executor_id="old",
                active_executor_generation=1,
            )
        )
        await session.commit()

    barrier = asyncio.Barrier(2)

    async def mark() -> None:
        async with factory() as session:
            await barrier.wait()
            await mark_executor_unavailable(
                session,
                conversation_id="conv",
                task_id="task",
                expected_executor_id="old",
                expected_generation=1,
                observed_at=datetime.now(UTC),
            )
            await session.commit()

    async def switch() -> None:
        async with factory() as session:
            await barrier.wait()
            await switch_task_and_conversation_active_executor(
                session,
                task_id="task",
                conversation_id="conv",
                active_executor_id="new",
            )
            await session.commit()

    outcomes = await asyncio.gather(mark(), switch(), return_exceptions=True)
    assert all(
        not isinstance(outcome, Exception) or isinstance(outcome, OperationalError)
        for outcome in outcomes
    )
    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "task"))
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_id == "conv")
        )
        assert task is not None and conversation is not None
        assert task.active_executor_id == conversation.active_executor_id
        assert task.active_executor_generation == conversation.active_executor_generation
        assert (
            task.active_executor_unavailable_since == conversation.active_executor_unavailable_since
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_two_task_authoritative_missing_selector_failovers_have_one_notice(
    tmp_path: object,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/pin-concurrent-selector.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="unique-selector@example.com", name="U", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="unique-selector-agent",
                owner_email="unique-selector@example.com",
                name="A",
            )
        )
        await session.flush()
        session.add(
            Conversation(
                conversation_id="unique-selector-conv",
                user_email="unique-selector@example.com",
                agent_id="unique-selector-agent",
                context_type="chat",
                active_executor_id="missing-selector",
                active_executor_source="selector_primary",
                active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=30),
                active_executor_generation=7,
            )
        )
        session.add(
            Task(
                task_id="unique-selector-task",
                title="T",
                created_by="unique-selector@example.com",
                agent_id="unique-selector-agent",
                active_executor_id="missing-selector",
                active_executor_source="selector_primary",
                active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=30),
                active_executor_generation=7,
            )
        )
        await session.commit()

    class _Dispatcher:
        calls = 0

        async def dispatch_pending(self, *, limit: int) -> None:
            self.calls += 1

    dispatcher = _Dispatcher()
    kwargs = dict(
        session_factory=factory,
        conversation_id="unique-selector-conv",
        task_id="unique-selector-task",
        pool=ExecutorPool(primary=[_sqlite_target("replacement-sqlite")]),
        active_executor_id="missing-selector",
        active_executor_expires_at=None,
        active_executor_generation=7,
        active_executor_unavailable_since=datetime.now(UTC) - timedelta(seconds=30),
        active_executor_source="selector_primary",
        ws_provider=_ReadyProvider(),
        retry_seconds=15,
        notice_dispatcher=dispatcher,
    )
    results = await asyncio.gather(
        ensure_active_executor_pin(**kwargs),
        ensure_active_executor_pin(**kwargs),
    )
    assert [result.active_executor_id for result in results] == [
        "replacement-sqlite",
        "replacement-sqlite",
    ]
    assert sum(result.notice is not None for result in results) == 1
    async with factory() as session:
        task = await session.scalar(select(Task).where(Task.task_id == "unique-selector-task"))
        transitions = await session.scalar(
            select(func.count()).select_from(ExecutorPinTransitionRow)
        )
        outbox = await session.scalar(select(func.count()).select_from(ExecutorPinNoticeOutboxRow))
        assert task is not None
        assert task.active_executor_id == "replacement-sqlite"
        assert task.active_executor_generation == 8
        assert transitions == 1
        assert outbox == 1
    assert dispatcher.calls == 1
    await engine.dispose()
