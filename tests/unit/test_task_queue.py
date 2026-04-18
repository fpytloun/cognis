"""Tests for the task queue and related DB queries."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cognis.store.database import create_engine, create_session_factory
from cognis.core.task_queue import TaskQueue
from cognis.store.models import Agent, Base, User
from cognis.store.queries import (
    add_task_dependency,
    create_step_run,
    create_task,
    fail_orphaned_running_step_runs,
    fail_running_step_runs_for_task,
    get_step_run,
    get_task,
    list_tasks_by_status,
    pick_ready_task,
    update_step_run,
    update_task_status,
    update_task_workflow_state,
)


async def _bootstrap_db(tmp_path: object) -> tuple[object, object]:
    """Create engine, run schema, seed user+agent."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)

    async with factory() as session:
        session.add(User(email="user@test.com", name="Test", role="admin"))
        await session.flush()
        session.add(
            Agent(
                agent_id="agent-1",
                owner_email="user@test.com",
                name="Agent",
            )
        )
        await session.commit()

    return engine, factory


@pytest.mark.asyncio
async def test_create_and_get_task(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Test Task",
                description="A test task",
                status="draft",
            )
            await session.commit()
            task_id = row.task_id

        async with factory() as session:
            task = await get_task(session, task_id)
            assert task is not None
            assert task.title == "Test Task"
            assert task.status == "draft"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recover_orphaned_running_step_runs_cleans_terminal_parent_leaks(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Failed task",
                status="failed",
                task_id="task_failed_recovery",
            )
            await create_step_run(
                session,
                task_id="task_failed_recovery",
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_failed_recovery",
            )
            await update_step_run(session, "sr_failed_recovery", status="running")
            await session.commit()

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(publish=lambda *_args, **_kwargs: None),
        )

        recovered = await queue.recover_orphaned_running_step_runs()

        assert recovered == 1
        async with factory() as session:
            step_run = await get_step_run(session, "sr_failed_recovery")
            assert step_run is not None and step_run.status == "failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fail_orphaned_running_step_runs_only_touches_terminal_tasks(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Failed task",
                status="failed",
                task_id="task_failed",
            )
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Running task",
                status="running",
                task_id="task_running",
            )
            await create_step_run(
                session,
                task_id="task_failed",
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_orphaned",
            )
            await create_step_run(
                session,
                task_id="task_running",
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_active",
            )
            await update_step_run(session, "sr_orphaned", status="running")
            await update_step_run(session, "sr_active", status="running")
            await session.commit()

        async with factory() as session:
            updated = await fail_orphaned_running_step_runs(session, datetime.now(UTC))
            await session.commit()
            assert updated == 1

        async with factory() as session:
            orphaned = await get_step_run(session, "sr_orphaned")
            active = await get_step_run(session, "sr_active")
            assert orphaned is not None and orphaned.status == "failed"
            assert active is not None and active.status == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fail_orphaned_running_step_runs_preserves_cancelled_parent_semantics(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Cancelled task",
                status="cancelled",
                task_id="task_cancelled",
            )
            await create_step_run(
                session,
                task_id="task_cancelled",
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_cancelled_orphan",
            )
            await update_step_run(session, "sr_cancelled_orphan", status="running")
            await session.commit()

        async with factory() as session:
            updated = await fail_orphaned_running_step_runs(session, datetime.now(UTC))
            await session.commit()
            assert updated == 1

        async with factory() as session:
            step_run = await get_step_run(session, "sr_cancelled_orphan")
            assert step_run is not None and step_run.status == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_status_transitions(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Status Test",
                status="draft",
            )
            task_id = row.task_id
            await session.commit()

        # draft → queued
        async with factory() as session:
            ok = await update_task_status(session, task_id, "queued")
            await session.commit()
            assert ok is True

        # queued → ready
        async with factory() as session:
            ok = await update_task_status(session, task_id, "ready")
            await session.commit()
            assert ok is True

        # ready → running
        async with factory() as session:
            ok = await update_task_status(session, task_id, "running", started_at=datetime.now(UTC))
            await session.commit()
            assert ok is True

        # Invalid: draft → completed (should fail because current is running)
        async with factory() as session:
            ok = await update_task_status(session, task_id, "completed")
            await session.commit()
            assert ok is True  # running → completed is valid
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pick_ready_task_cas(tmp_path: object) -> None:
    """Test that pick_ready_task uses compare-and-swap correctly."""
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        # Create a ready task
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Ready Task",
                status="ready",
                priority=5,
            )
            await session.commit()

        # Pick it
        async with factory() as session:
            task = await pick_ready_task(session)
            await session.commit()
            assert task is not None
            assert task.status == "running"

        # Try to pick again — should be None
        async with factory() as session:
            task2 = await pick_ready_task(session)
            await session.commit()
            assert task2 is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_dependencies_cycle_detection(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session, created_by="user@test.com", agent_id="agent-1", title="A", task_id="t_a"
            )
            await create_task(
                session, created_by="user@test.com", agent_id="agent-1", title="B", task_id="t_b"
            )
            await create_task(
                session, created_by="user@test.com", agent_id="agent-1", title="C", task_id="t_c"
            )
            await session.commit()

        # A → B → C is fine
        async with factory() as session:
            await add_task_dependency(session, "t_b", "t_a")
            await add_task_dependency(session, "t_c", "t_b")
            await session.commit()

        # C → A would create a cycle
        async with factory() as session:
            with pytest.raises(ValueError, match="cycle"):
                await add_task_dependency(session, "t_a", "t_c")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_self_dependency_rejected(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Self",
                task_id="t_self",
            )
            await session.commit()

        async with factory() as session:
            with pytest.raises(ValueError, match="itself"):
                await add_task_dependency(session, "t_self", "t_self")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_state_persistence(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Workflow Task",
                status="running",
            )
            task_id = row.task_id
            await session.commit()

        # Persist workflow state
        state = {"current_step_index": 2, "step_outputs": {"plan": {"summary": "ok"}}}
        async with factory() as session:
            ok = await update_task_workflow_state(session, task_id, state)
            await session.commit()
            assert ok is True

        # Verify it was persisted
        async with factory() as session:
            task = await get_task(session, task_id)
            assert task is not None
            assert task.workflow_state is not None
            assert task.workflow_state["current_step_index"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_tasks_by_status(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Draft 1",
                status="draft",
            )
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Queued 1",
                status="queued",
                priority=5,
            )
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Queued 2",
                status="queued",
                priority=10,
            )
            await session.commit()

        async with factory() as session:
            queued = await list_tasks_by_status(session, ["queued"])
            assert len(queued) == 2
            # Higher priority first
            assert queued[0].priority == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_active_step_runs_marks_paused_and_running_rows(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Cancelable task",
                status="running",
                task_id="task_cancel",
            )
            await create_step_run(
                session,
                task_id=row.task_id,
                step_name="implement",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_running",
            )
            await create_step_run(
                session,
                task_id=row.task_id,
                step_name="review",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr_paused",
            )
            await update_step_run(session, "sr_running", status="running")
            await update_step_run(session, "sr_paused", status="running")
            await update_step_run(session, "sr_paused", status="paused")
            await session.commit()

        async with factory() as session:
            updated = await fail_running_step_runs_for_task(
                session,
                "task_cancel",
                datetime.now(UTC),
                final_status="cancelled",
            )
            await session.commit()
            assert updated == 2

        async with factory() as session:
            running = await get_step_run(session, "sr_running")
            paused = await get_step_run(session, "sr_paused")
            assert running is not None and running.status == "cancelled"
            assert paused is not None and paused.status == "cancelled"
    finally:
        await engine.dispose()
