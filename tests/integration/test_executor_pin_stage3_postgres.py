from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import schema as sa_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.core.executor_pin_lifecycle import ensure_active_executor_pin
from cognis.core.executor_pool import ExecutorAvailability, ExecutorPool, ResolvedExecutorTarget
from cognis.store.database import create_session_factory
from cognis.store.models import Agent, Base, Conversation, Task, User
from cognis.store.queries import (
    mark_executor_unavailable,
    switch_task_and_conversation_active_executor,
)


class _ReadyConnection:
    connected = True


class _ReadyProvider:
    def get_connection(self, executor_id: str) -> _ReadyConnection | None:
        return _ReadyConnection() if executor_id.startswith("replacement-") else None


def _target(executor_id: str) -> ResolvedExecutorTarget:
    return ResolvedExecutorTarget(
        executor_id=executor_id,
        executor_type="websocket",
        is_primary=True,
        selection_source="selector",
        description=None,
        state=ExecutorAvailability.USABLE,
        row=object(),
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


def _url() -> str:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.mark.asyncio
async def test_concurrent_mark_and_switch_preserve_projection() -> None:
    suffix = uuid4().hex
    schema_name = f"cognis_executor_pin_{suffix}"
    conversation_id = f"stage3-conversation-{suffix}"
    task_id = f"stage3-task-{suffix}"
    user_email = f"stage3-{suffix}@example.com"
    agent_id = f"stage3-agent-{suffix}"
    url = _url()
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        async with factory() as session:
            session.add(User(email=user_email, name="Stage 3", role="user"))
            await session.flush()
            session.add(Agent(agent_id=agent_id, owner_email=user_email, name="Stage 3"))
            await session.flush()
            session.add(
                Conversation(
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                    context_type="chat",
                    active_executor_id="old",
                    active_executor_generation=1,
                )
            )
            session.add(
                Task(
                    task_id=task_id,
                    title="Stage 3",
                    created_by=user_email,
                    agent_id=agent_id,
                    active_executor_id="old",
                    active_executor_generation=1,
                )
            )
            await session.commit()

        async def mark() -> None:
            async with factory() as session:
                await mark_executor_unavailable(
                    session,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    expected_executor_id="old",
                    expected_generation=1,
                    observed_at=datetime.now(UTC),
                )
                await session.commit()

        async def switch() -> None:
            async with factory() as session:
                await switch_task_and_conversation_active_executor(
                    session,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    active_executor_id="new",
                )
                await session.commit()

        await asyncio.gather(mark(), switch())
        async with factory() as session:
            task = await session.scalar(select(Task).where(Task.task_id == task_id))
            conversation = await session.scalar(
                select(Conversation).where(Conversation.conversation_id == conversation_id)
            )
            assert task is not None and conversation is not None
            assert task.active_executor_id == conversation.active_executor_id
            assert task.active_executor_generation == conversation.active_executor_generation
            assert (
                task.active_executor_unavailable_since
                == conversation.active_executor_unavailable_since
            )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()


@pytest.mark.asyncio
async def test_two_controllers_missing_selector_have_one_cas_transition_and_outbox() -> None:
    suffix = uuid4().hex
    schema_name = f"cognis_executor_pin_failover_{suffix}"
    conversation_id = f"selector-conversation-{suffix}"
    task_id = f"selector-task-{suffix}"
    user_email = f"selector-{suffix}@example.com"
    agent_id = f"selector-agent-{suffix}"
    url = _url()
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa_schema.CreateSchema(schema_name))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema_name}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = create_session_factory(engine)
        observed_at = datetime.now(UTC)
        async with factory() as session:
            session.add(User(email=user_email, name="Selector", role="user"))
            await session.flush()
            session.add(Agent(agent_id=agent_id, owner_email=user_email, name="Selector"))
            await session.flush()
            session.add(
                Conversation(
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id=agent_id,
                    context_type="chat",
                    active_executor_id="missing-selector",
                    active_executor_source="selector_primary",
                    active_executor_unavailable_since=observed_at,
                    active_executor_generation=3,
                )
            )
            session.add(
                Task(
                    task_id=task_id,
                    title="Selector failover",
                    created_by=user_email,
                    agent_id=agent_id,
                    active_executor_id="missing-selector",
                    active_executor_source="selector_primary",
                    active_executor_unavailable_since=observed_at,
                    active_executor_generation=3,
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
            conversation_id=conversation_id,
            task_id=task_id,
            pool=ExecutorPool(primary=[_target(f"replacement-{suffix}")]),
            active_executor_id="missing-selector",
            active_executor_expires_at=None,
            active_executor_generation=3,
            active_executor_unavailable_since=observed_at,
            active_executor_source="selector_primary",
            ws_provider=_ReadyProvider(),
            retry_seconds=1,
            notice_dispatcher=dispatcher,
            now=observed_at + timedelta(seconds=2),
        )
        results = await asyncio.gather(
            ensure_active_executor_pin(**kwargs),
            ensure_active_executor_pin(**kwargs),
        )
        assert [result.active_executor_id for result in results] == [
            f"replacement-{suffix}",
            f"replacement-{suffix}",
        ]
        assert sum(result.notice is not None for result in results) == 1
        from sqlalchemy import func

        from cognis.store.models import ExecutorPinNoticeOutboxRow, ExecutorPinTransitionRow

        async with factory() as session:
            task = await session.scalar(select(Task).where(Task.task_id == task_id))
            transitions = await session.scalar(
                select(func.count()).select_from(ExecutorPinTransitionRow)
            )
            outbox = await session.scalar(
                select(func.count()).select_from(ExecutorPinNoticeOutboxRow)
            )
            assert task is not None
            assert task.active_executor_id == f"replacement-{suffix}"
            assert task.active_executor_generation == 4
            assert transitions == 1
            assert outbox == 1
        assert dispatcher.calls == 1
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa_schema.DropSchema(schema_name, cascade=True))
        await admin.dispose()
