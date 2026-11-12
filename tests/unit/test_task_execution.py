from __future__ import annotations

import asyncio

import pytest

import cognis.core.task_execution as task_execution_module
from cognis.core.task_execution import (
    StaleTaskExecutionOwner,
    TaskExecutionFence,
    TaskExecutionStore,
)
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Task, User
from cognis.store.queries import create_task


async def _database(tmp_path: object) -> tuple[object, object]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/task-execution.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="admin"))
        await session.flush()
        session.add(Agent(agent_id="agent-1", owner_email="owner@example.com", name="Agent"))
        await create_task(
            session,
            task_id="task-1",
            created_by="owner@example.com",
            agent_id="agent-1",
            title="First",
            status="ready",
        )
        await create_task(
            session,
            task_id="task-2",
            created_by="owner@example.com",
            agent_id="agent-1",
            title="Second",
            status="ready",
        )
        await session.commit()
    return engine, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "cancelled", "paused", "failed"])
async def test_shutdown_preserves_remote_work_but_explicit_stop_does_not(tmp_path, status):
    from cognis.providers.executor.delivery import preserve_task_execution_on_interrupt

    engine, factory = await _database(tmp_path)
    token = None
    fence = None
    try:
        store = TaskExecutionStore(
            factory, owner_id="controller-shutdown", max_active_global=1, max_active_per_agent=1
        )
        claim = await store.claim_ready()
        assert claim is not None
        fence = TaskExecutionFence(store, claim, asyncio.Event())
        token = task_execution_module.bind_task_execution_fence(fence)
        async with factory() as db:
            task = await db.get(Task, claim.task_id)
            task.status = status
            await db.commit()
        fence.cancel_event.set()
        assert await preserve_task_execution_on_interrupt() is (status == "running")
    finally:
        if token is not None:
            task_execution_module.reset_task_execution_fence(token)
        if fence is not None:
            await fence.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_claim_enforces_distributed_capacity(tmp_path: object) -> None:
    engine, factory = await _database(tmp_path)
    try:
        first_store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
        )
        second_store = TaskExecutionStore(
            factory,
            owner_id="controller-b:boot-b",
            max_active_global=1,
            max_active_per_agent=1,
        )

        first, second = await asyncio.gather(first_store.claim_ready(), second_store.claim_ready())

        claims = [claim for claim in (first, second) if claim is not None]
        assert len(claims) == 1
        assert claims[0].task_id in {"task-1", "task-2"}
        assert claims[0].agent_id == "agent-1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_slot_is_not_reentrant_within_one_controller(tmp_path: object) -> None:
    engine, factory = await _database(tmp_path)
    try:
        store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
        )
        first = await store.claim_ready()
        second = await store.claim_ready()
        assert first is not None
        assert second is None
        assert await store.renew(first) is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_paused_task_releases_capacity_and_reacquires_before_resume(
    tmp_path: object,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        first_store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
        )
        second_store = TaskExecutionStore(
            factory,
            owner_id="controller-b:boot-b",
            max_active_global=1,
            max_active_per_agent=1,
        )
        first = await first_store.claim_ready()
        assert first is not None
        paused = await first_store.release_capacity(first)
        assert paused is not None
        assert paused.has_capacity is False

        second = await second_store.claim_ready()
        assert second is not None
        assert await first_store.reacquire_capacity(paused) is None

        await second_store.release(second)
        resumed = await first_store.reacquire_capacity(paused)
        assert resumed is not None
        assert resumed.has_capacity is True
        assert resumed.task_lease.fencing_token == first.task_lease.fencing_token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_already_running_controller_takes_over_paused_task_after_expiry(
    tmp_path: object,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        owner = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
            ttl_seconds=2,
        )
        standby = TaskExecutionStore(
            factory,
            owner_id="controller-b:boot-b",
            max_active_global=1,
            max_active_per_agent=1,
            ttl_seconds=60,
        )
        claim = await owner.claim_ready()
        assert claim is not None
        async with factory() as session:
            task = await session.get(Task, claim.task_id)
            assert task is not None
            task.status = "paused"
            await session.commit()
        paused = await owner.release_capacity(claim)
        assert paused is not None

        assert await standby.claim_paused(claim.task_id) is None
        await asyncio.sleep(2.1)
        takeover = await standby.claim_paused(claim.task_id)
        assert takeover is not None
        assert takeover.task_lease.fencing_token > claim.task_lease.fencing_token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_owner_cannot_settle_after_takeover(tmp_path: object) -> None:
    engine, factory = await _database(tmp_path)
    try:
        old_store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
            ttl_seconds=0.05,
        )
        new_store = TaskExecutionStore(
            factory,
            owner_id="controller-b:boot-b",
            max_active_global=1,
            max_active_per_agent=1,
            ttl_seconds=60,
        )
        old_claim = await old_store.claim_ready()
        assert old_claim is not None
        await asyncio.sleep(0.08)
        new_claim = await new_store.claim_existing(old_claim.task_id, statuses={"running"})
        assert new_claim is not None
        assert new_claim.task_lease.fencing_token > old_claim.task_lease.fencing_token

        old_fence = TaskExecutionFence(old_store, old_claim, asyncio.Event())
        async with factory() as session:
            with pytest.raises(StaleTaskExecutionOwner):
                await old_fence.assert_current(session)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_renewal_error_marks_fence_lost_and_cancels(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
        )
        claim = await store.claim_ready()
        assert claim is not None

        async def _fail_renew(_claim: object) -> None:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(store, "renew", _fail_renew)
        monkeypatch.setattr(task_execution_module, "TASK_LEASE_RENEW_SECONDS", 0.01)
        cancelled = asyncio.Event()
        fence = TaskExecutionFence(store, claim, cancelled)
        fence.start()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        async with factory() as session:
            with pytest.raises(StaleTaskExecutionOwner):
                await fence.assert_current(session)
        await fence.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_dispatch_boundaries_persist_metadata_before_send(
    tmp_path: object,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
        )
        claim = await store.claim_ready()
        assert claim is not None
        fence = TaskExecutionFence(store, claim, asyncio.Event())
        await fence.record_tool_dispatch(
            session_id="sess-task",
            turn_id="turn-task",
            descriptors=[
                {
                    "call_id": "call-1",
                    "tool_name": "bash",
                    "dispatch_state": "pending",
                    "arguments": {"secret": "must-not-be-copied"},
                    "frozen": {"arguments": {"secret": "must-not-be-copied"}},
                }
            ],
        )
        await fence.bind_tool_dispatch(
            "call-1",
            "executor-task",
            "instance-task",
            session_id="sess-task",
            turn_id="turn-task",
            dispatch_state="bound",
        )
        async with factory() as db:
            task = await db.get(Task, claim.task_id)
            batches = task.result_data["execution_recovery"]
            descriptor = batches["sess-task:turn-task"]["tool_calls"][0]
            assert descriptor == {
                "call_id": "call-1",
                "canonical_turn_id": "turn-task",
                "tool_name": "bash",
                "dispatch_state": "bound",
                "executor_id": "executor-task",
                "executor_instance_id": "instance-task",
            }
        await fence.complete_tool_dispatch(
            session_id="sess-task", turn_id="turn-task", call_ids=["call-1"]
        )
        async with factory() as db:
            task = await db.get(Task, claim.task_id)
            assert task.result_data["execution_recovery"] == {}
        await fence.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_or_superseded_attempt_invalidates_fence_immediately(
    tmp_path: object,
) -> None:
    engine, factory = await _database(tmp_path)
    try:
        store = TaskExecutionStore(
            factory,
            owner_id="controller-a:boot-a",
            max_active_global=1,
            max_active_per_agent=1,
        )
        claim = await store.claim_ready()
        assert claim is not None
        fence = TaskExecutionFence(store, claim, asyncio.Event())

        async with factory() as session:
            task = await session.get(Task, claim.task_id)
            assert task is not None
            task.status = "cancelled"
            await session.commit()

        assert await store.renew(claim) is None
        with pytest.raises(StaleTaskExecutionOwner):
            await fence.assert_current()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_workflow_provider_deferral_uses_real_task_fence_and_compare_and_set(
    tmp_path: object,
    cancelled: bool,
) -> None:
    from cognis.core.task_execution import bind_task_execution_fence, reset_task_execution_fence
    from cognis.core.workflow_engine import WorkflowEngine
    from cognis.models.workflow import StepDefinition, StepOutput, Workflow, WorkflowState
    from cognis.store.queries import get_task

    engine, factory = await _database(tmp_path)
    try:
        store = TaskExecutionStore(
            factory,
            owner_id="controller-a:provider-recovery",
            max_active_global=1,
            max_active_per_agent=1,
        )
        claim = await store.claim_ready()
        assert claim is not None
        async with factory() as session:
            task = await get_task(session, claim.task_id)
        assert task is not None
        if cancelled:
            async with factory() as session:
                row = await session.get(Task, claim.task_id)
                row.status = "cancelled"
                await session.commit()
        state = WorkflowState()
        step = StepDefinition(name="run", type="run", prompt="Do work")
        workflow = Workflow(workflow_id="wf:provider-recovery", name="Recovery", steps=[step])
        failure = StepOutput(
            summary="Provider rate limited",
            error="Provider reset in 600 seconds",
            metadata={"model_error": {"transient": True, "retry_after_seconds": 600}},
        )
        workflow_engine = object.__new__(WorkflowEngine)
        workflow_engine._session_factory = factory
        fence = TaskExecutionFence(store, claim, asyncio.Event())
        token = bind_task_execution_fence(fence)
        try:
            if cancelled:
                with pytest.raises(StaleTaskExecutionOwner):
                    await workflow_engine._defer_step_recovery(task, step, state, workflow, failure)
            else:
                assert await workflow_engine._defer_step_recovery(
                    task,
                    step,
                    state,
                    workflow,
                    failure,
                )
        finally:
            reset_task_execution_fence(token)
        async with factory() as session:
            row = await session.get(Task, claim.task_id)
            assert row.status == ("cancelled" if cancelled else "ready")
            if not cancelled:
                assert row.result_summary == failure.error
                assert row.workflow_state["loop_iterations"] == {"recovery:run": 1}
                assert row.scheduled_for is not None
    finally:
        await engine.dispose()
