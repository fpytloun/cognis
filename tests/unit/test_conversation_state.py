from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cognis.core.conversation_state import (
    linked_conversation_ids_for_task,
    snapshot_for_conversation,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, NotificationRow, User
from cognis.store.queries import (
    create_session,
    create_step_run,
    create_task,
    replace_conversation_todos,
)


async def _factory(tmp_path: object):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/conversation-state.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="user@test.com", name="User", role="admin"))
        session.add(User(email="other@test.com", name="Other", role="user"))
        await session.flush()
        session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="Agent"))
        await session.commit()
    return engine, factory


@pytest.mark.asyncio
async def test_normal_conversation_snapshot_has_no_task(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            session.add(
                Conversation(
                    conversation_id="conv_normal",
                    user_email="user@test.com",
                    agent_id="agent-1",
                    context_type="web",
                )
            )
            await session.commit()
            snapshot = await snapshot_for_conversation(
                session,
                user_email="user@test.com",
                conversation_id="conv_normal",
            )
        assert snapshot is not None
        assert snapshot.conversation_kind == "normal"
        assert snapshot.task is None
        assert snapshot.pending.notification_types == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_normal_conversation_snapshot_projects_conversation_todos(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            session.add(
                Conversation(
                    conversation_id="conv_normal",
                    user_email="user@test.com",
                    agent_id="agent-1",
                    context_type="web",
                    active_session_id="sess_1",
                )
            )
            await create_session(
                session,
                session_id="sess_1",
                conversation_id="conv_normal",
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await replace_conversation_todos(
                session,
                "conv_normal",
                [
                    {
                        "content": "Keep TODO state visible",
                        "status": "in_progress",
                        "priority": "high",
                    }
                ],
            )
            await session.commit()
            snapshot = await snapshot_for_conversation(
                session,
                user_email="user@test.com",
                conversation_id="conv_normal",
            )
        assert snapshot is not None
        assert snapshot.task is None
        assert snapshot.active_session.todos[0].content == "Keep TODO state visible"
        assert snapshot.active_session.todos[0].status == "in_progress"
        assert snapshot.active_session.todos[0].priority == "high"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_step_conversation_snapshot_projects_todos_and_pending(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            session.add(
                Conversation(
                    conversation_id="conv_step",
                    user_email="user@test.com",
                    agent_id="agent-1",
                    context_type="web",
                )
            )
            task = await create_task(
                session,
                task_id="task_1",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Task",
                status="running",
            )
            step = await create_step_run(
                session,
                step_run_id="sr_1",
                task_id=task.task_id,
                step_name="implement",
                step_type="agent",
                agent_id="agent-1",
                conversation_id="conv_step",
                status="running",
                started_at=datetime.now(UTC),
            )
            step.todos = [
                {"content": "Build backend state", "status": "in_progress", "priority": "high"}
            ]
            session.add(
                NotificationRow(
                    notification_id="ntf_1",
                    notification_type="step_question",
                    user_email="user@test.com",
                    conversation_id="conv_step",
                    task_id=task.task_id,
                    step_run_id=step.step_run_id,
                    status="pending",
                    payload={"question": "Continue?", "options": [{"label": "yes"}]},
                )
            )
            await session.commit()
            snapshot = await snapshot_for_conversation(
                session,
                user_email="user@test.com",
                conversation_id="conv_step",
            )
        assert snapshot is not None
        assert snapshot.conversation_kind == "task_step"
        assert snapshot.linked_task_id == "task_1"
        assert snapshot.task is not None
        assert snapshot.task.relevant_step is not None
        assert snapshot.task.relevant_step.todos[0].content == "Build backend state"
        assert snapshot.pending.notification_types == ["step_question"]
        assert snapshot.pending.pending_input is not None
        assert snapshot.pending.pending_input.question == "Continue?"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_forked_task_step_validates_step_ownership(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task_1",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Task",
            )
            await create_task(
                session,
                task_id="task_other",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Other",
            )
            await create_step_run(
                session,
                step_run_id="sr_other",
                task_id="task_other",
                step_name="other",
                step_type="agent",
                agent_id="agent-1",
            )
            session.add(
                Conversation(
                    conversation_id="conv_fork",
                    user_email="user@test.com",
                    agent_id="agent-1",
                    context_type="web",
                    context_data={
                        "forked_from": "task_step",
                        "task_id": "task_1",
                        "step_run_id": "sr_other",
                    },
                )
            )
            await session.commit()
            snapshot = await snapshot_for_conversation(
                session,
                user_email="user@test.com",
                conversation_id="conv_fork",
            )
        assert snapshot is not None
        assert snapshot.conversation_kind == "normal"
        assert snapshot.task is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_linked_conversation_fanout_includes_step_and_forks(tmp_path: object) -> None:
    engine, factory = await _factory(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task_1",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Task",
            )
            await create_step_run(
                session,
                step_run_id="sr_1",
                task_id="task_1",
                step_name="implement",
                step_type="agent",
                agent_id="agent-1",
                conversation_id="conv_step",
            )
            session.add_all(
                [
                    Conversation(
                        conversation_id="conv_step",
                        user_email="user@test.com",
                        agent_id="agent-1",
                        context_type="web",
                    ),
                    Conversation(
                        conversation_id="conv_task",
                        user_email="user@test.com",
                        agent_id="agent-1",
                        context_type="web",
                        context_data={"forked_from": "task", "task_id": "task_1"},
                    ),
                    Conversation(
                        conversation_id="conv_other_user",
                        user_email="other@test.com",
                        agent_id="agent-1",
                        context_type="web",
                        context_data={"forked_from": "task", "task_id": "task_1"},
                    ),
                ]
            )
            await session.commit()
            ids = await linked_conversation_ids_for_task(
                session,
                user_email="user@test.com",
                task_id="task_1",
            )
        assert ids == ["conv_step", "conv_task"]
    finally:
        await engine.dispose()
