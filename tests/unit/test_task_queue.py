"""Tests for the task queue and related DB queries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from cognis.core.agent_registry import AgentRegistry
from cognis.core.events import Event, EventBus, EventType
from cognis.core.task_execution import TaskExecutionClaim
from cognis.core.task_queue import TaskQueue, TaskRerunResult, _row_to_task_model
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import StepDefinition, Workflow, WorkflowState
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, StepRun, User
from cognis.store.queries import (
    add_task_dependency,
    create_conversation,
    create_session,
    create_skill,
    create_skill_version,
    create_step_run,
    create_task,
    fail_orphaned_running_step_runs,
    fail_running_step_runs_for_task,
    get_step_run,
    get_task,
    get_task_dependencies,
    list_tasks_by_status,
    list_tasks_for_agent,
    pick_ready_task,
    set_current_version,
    update_conversation_active_session,
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
@pytest.mark.parametrize(
    ("origin", "creator", "expected"),
    [
        ("owner_configured", None, "owner_configured"),
        ("owner_configured", "agent-1", "agent_composed"),
        (None, None, None),
    ],
)
async def test_task_creation_origin_round_trip(tmp_path, origin, creator, expected):
    engine, factory = await _bootstrap_db(tmp_path)
    queue = TaskQueue(
        session_factory=factory,
        workflow_engine=AsyncMock(),
        workflow_registry=WorkflowRegistry(factory),
        event_bus=EventBus(),
    )
    try:
        task = await queue.create_draft(
            created_by="user@test.com",
            agent_id="agent-1",
            title="Origin",
            creation_origin=origin,
            created_by_agent_id=creator,
        )
        assert task.creation_origin == expected
        async with factory() as db:
            row = await get_task(db, task.task_id)
            assert row.creation_origin == expected
            assert _row_to_task_model(row).creation_origin == expected
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["deleted", "agent_edit", "owner_edit", "matched"])
async def test_agent_workflow_edit_revokes_schedule_and_detached_task_origin(tmp_path, scenario):
    from cognis.core.workflow_management import create_user_workflow, update_user_workflow
    from cognis.core.workflow_step_scope import WorkflowStepScope, refresh_scope_origin
    from cognis.models.workflow import Workflow, WorkflowState, pin_effective_workflow
    from cognis.store.queries import create_schedule, delete_schedule, get_schedule

    engine, factory = await _bootstrap_db(tmp_path)
    try:
        workflow = await create_user_workflow(
            session_factory=factory,
            owner_email="user@test.com",
            payload={
                "name": "Inspect",
                "steps": [
                    {
                        "name": "inspect",
                        "type": "tool_call",
                        "tool_call": {"tool": "bash", "args": {"command": "true"}},
                    }
                ],
            },
        )
        async with factory() as db:
            await create_schedule(
                db,
                schedule_id="sched_origin",
                task_template={"title": "Inspect"},
                name="Owner schedule",
                agent_id="agent-1",
                schedule_type="interval",
                interval_seconds=60,
                created_by="user@test.com",
                workflow_id=workflow.workflow_id,
                creation_origin="owner_configured",
            )
            await db.commit()
        state = pin_effective_workflow(
            WorkflowState(), Workflow.model_validate(workflow.definition)
        )
        if scenario != "matched":
            await update_user_workflow(
                session_factory=factory,
                owner_email="user@test.com",
                workflow_id=workflow.workflow_id,
                agent_composed=scenario != "owner_edit",
                payload={
                    "steps": [
                        {
                            "name": "inspect",
                            "type": "tool_call",
                            "tool_call": {"tool": "bash", "args": {"command": "pwd"}},
                        }
                    ]
                },
            )
        async with factory() as db:
            schedule = await get_schedule(db, "sched_origin")
            if scenario in {"deleted", "agent_edit"}:
                assert schedule.creation_origin == "agent_composed"
            row = await create_task(
                db,
                task_id="task_stale_origin",
                title="Scheduled",
                created_by="user@test.com",
                agent_id="agent-1",
                workflow_id=workflow.workflow_id,
                source_type="scheduler",
                source_ref="sched_origin",
                creation_origin="owner_configured",
            )
            task = _row_to_task_model(row)
            if scenario == "deleted":
                # The one-shot scheduler removes the source after creating its
                # task from the stale, detached owner-configured snapshot.
                await delete_schedule(db, "sched_origin")
            await db.commit()
        if scenario == "deleted":
            current = await WorkflowRegistry(factory).get(workflow.workflow_id)
            state = pin_effective_workflow(WorkflowState(), current)
        task.workflow_state = state
        scope_task, reason = await refresh_scope_origin(task, factory)
        expected = "owner_configured" if scenario == "matched" else "agent_composed"
        assert scope_task.creation_origin == expected
        assert reason == (
            "schedule_missing"
            if scenario == "deleted"
            else None
            if scenario == "matched"
            else "schedule_digest_mismatch"
        )
        pinned = Workflow.model_validate(state.effective_workflow_definition)
        scope = WorkflowStepScope.build(
            task=scope_task,
            workflow=pinned,
            state=state,
            step=pinned.steps[0],
            session_id="sess_scope",
            step_run_id="run_scope",
            logical_call_id="call_scope",
            rendered_args=pinned.steps[0].tool_call.args,
        )
        declaration = scope.declaration(
            session_id="sess_scope",
            call_id="call_scope",
            tool="bash",
            args=pinned.steps[0].tool_call.args,
            executor_id="fixture-executor",
        )
        assert declaration.authorized_by == expected
        assert "scope_origin_reason" not in declaration.model_dump()
        if scenario == "deleted":
            from cognis.core.tool_router import ToolRouter
            from cognis.models.agent import AgentDefinition
            from cognis.models.session import SessionModel
            from cognis.models.tool import (
                EvaluationResult,
                NativeToolDefinition,
                ToolCall,
                ToolSource,
            )
            from cognis.tools.registry import RegisteredTool, ToolRegistry

            guardrails = AsyncMock()
            guardrails.evaluate.return_value = EvaluationResult(
                decision="escalate",
                call_id="eval_scope",
                reasoning="Agent-composed workflow scope does not grant permission",
            )
            registry = ToolRegistry()
            registry.register(
                RegisteredTool(
                    definition=NativeToolDefinition(
                        name="bash",
                        description="Execute",
                        parameters={"type": "object"},
                        source=ToolSource(type="builtin"),
                    )
                )
            )
            executor = AsyncMock()
            result = await ToolRouter(guardrails).execute(
                ToolCall(
                    call_id="call_scope", name="bash", arguments=pinned.steps[0].tool_call.args
                ),
                agent=AgentDefinition(agent_id="agent-1", name="Test", owner_email="user@test.com"),
                session=SessionModel(
                    session_id="sess_scope",
                    conversation_id="conv_scope",
                    agent_id="agent-1",
                    user_email="user@test.com",
                ),
                registry=registry,
                executor=executor,
                workflow_step_scope=scope,
                workflow_executor_id="fixture-executor",
            )
            assert (
                guardrails.declare_workflow_step.call_args.args[0].authorized_by == "agent_composed"
            )
            assert result.metadata["evaluation"]["decision"] == "escalate"
            executor.tool_execute.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_queue_remote_wakeup_and_idle_attempt_reduction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    queue = TaskQueue(
        session_factory=AsyncMock(),
        workflow_engine=AsyncMock(),
        workflow_registry=AsyncMock(),
        event_bus=bus,
    )
    assert queue._poll_interval == 15
    picks = AsyncMock()
    monkeypatch.setattr(
        queue._execution_store, "next_scheduled_delay", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(queue, "_try_pick_and_run", picks)
    monkeypatch.setattr(queue, "recover_stale_tasks", AsyncMock())
    monkeypatch.setattr(queue, "recover_paused_tasks", AsyncMock())
    monkeypatch.setattr(
        "cognis.core.invariants.task_dispatches.reconcile_cancelled_dispatches",
        AsyncMock(return_value=0),
    )
    await queue.start()
    try:
        await asyncio.sleep(0.02)
        assert picks.await_count == 1  # Startup recheck does not wait for a signal.
        await asyncio.sleep(1.05)
        assert picks.await_count == 1  # No old one-second idle claim attempt.
        await bus.publish(
            Event(
                type=EventType.CLUSTER_SCOPE_INVALIDATED,
                data={"kind": "task_queue_changed", "scope": {}},
            )
        )
        await asyncio.sleep(0.02)
        assert picks.await_count == 2
    finally:
        await queue.stop()
    assert not bus._handlers[EventType.CLUSTER_SCOPE_INVALIDATED]


@pytest.mark.asyncio
async def test_queue_dropped_signal_fallback_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = TaskQueue(
        session_factory=AsyncMock(),
        workflow_engine=AsyncMock(),
        workflow_registry=AsyncMock(),
        event_bus=EventBus(),
        poll_interval_seconds=0.03,
    )
    picks = AsyncMock()
    monkeypatch.setattr(
        queue._execution_store, "next_scheduled_delay", AsyncMock(return_value=None)
    )
    recovery = AsyncMock()
    monkeypatch.setattr(queue, "_try_pick_and_run", picks)
    monkeypatch.setattr(queue, "recover_stale_tasks", recovery)
    monkeypatch.setattr(queue, "recover_paused_tasks", AsyncMock())
    monkeypatch.setattr(
        "cognis.core.invariants.task_dispatches.reconcile_cancelled_dispatches",
        AsyncMock(return_value=0),
    )
    await queue.start()
    try:
        await asyncio.sleep(0.11)
        assert picks.await_count >= 3
        assert recovery.await_count == 1
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_queue_error_backoff_is_interruptible(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = TaskQueue(
        session_factory=AsyncMock(),
        workflow_engine=AsyncMock(),
        workflow_registry=AsyncMock(),
        event_bus=EventBus(),
    )
    failed = asyncio.Event()
    retried = asyncio.Event()

    async def pick() -> None:
        if not failed.is_set():
            failed.set()
            raise RuntimeError("Transient test database failure")
        retried.set()

    monkeypatch.setattr(queue, "_try_pick_and_run", pick)
    monkeypatch.setattr(queue, "recover_stale_tasks", AsyncMock())
    monkeypatch.setattr(queue, "recover_paused_tasks", AsyncMock())
    monkeypatch.setattr(
        "cognis.core.invariants.task_dispatches.reconcile_cancelled_dispatches",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        queue._execution_store, "next_scheduled_delay", AsyncMock(return_value=None)
    )
    await queue.start()
    try:
        await asyncio.wait_for(failed.wait(), 1)
        queue._wake_event.set()
        await asyncio.wait_for(retried.wait(), 0.5)
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_ready_publication_observes_committed_state(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Commit ordering",
                description="Queue wakeup",
                status="queued",
            )
            await session.commit()
            task_id = row.task_id
        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=AsyncMock(),
            workflow_registry=AsyncMock(),
            event_bus=EventBus(),
        )

        async def published(*args: object, **kwargs: object) -> None:
            async with factory() as session:
                persisted = await get_task(session, task_id)
                assert persisted.status == "ready"

        publish = AsyncMock(side_effect=published)
        queue.cluster_signals = SimpleNamespace(publish=publish)
        assert await queue._try_transition_to_ready(task_id)
        publish.assert_awaited_once()
        assert queue._wake_event.is_set()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_already_ready_submission_publishes_queue_hint(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=AsyncMock(),
            workflow_registry=WorkflowRegistry(factory),
            event_bus=EventBus(),
        )
        publish = AsyncMock()
        queue.cluster_signals = SimpleNamespace(publish=publish, publish_task_change=AsyncMock())
        task = await queue.submit(
            created_by="user@test.com",
            agent_id="agent-1",
            title="Ready submission",
            status="ready",
        )
        publish.assert_awaited_once()
        assert publish.await_args.args[0] == "task_queue_changed"
        async with factory() as session:
            assert (await get_task(session, task.task_id)).status == "ready"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_coalesced_wakeup_drains_ready_burst(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    queue = TaskQueue(
        session_factory=factory,
        workflow_engine=AsyncMock(),
        workflow_registry=AsyncMock(),
        event_bus=EventBus(),
    )
    launched: list[str] = []
    monkeypatch.setattr(
        queue, "_launch_claimed_task_run", lambda task, claim: launched.append(task.task_id)
    )
    monkeypatch.setattr(queue, "recover_stale_tasks", AsyncMock())
    monkeypatch.setattr(queue, "recover_paused_tasks", AsyncMock())
    try:
        async with factory() as session:
            for number in range(3):
                await create_task(
                    session,
                    created_by="user@test.com",
                    agent_id="agent-1",
                    title=f"Burst {number}",
                    description="Coalesced wake",
                    status="ready",
                )
            await session.commit()
        await queue.start()
        async with asyncio.timeout(2):
            while len(launched) != 3:
                await asyncio.sleep(0.01)
        assert len(set(launched)) == 3
    finally:
        queue._stop_event.set()
        queue._wake_event.set()
        if queue._drain_task is not None:
            await queue._drain_task
        await queue.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_ready_task_wakes_at_deadline(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    queue = TaskQueue(
        session_factory=factory,
        workflow_engine=AsyncMock(),
        workflow_registry=AsyncMock(),
        event_bus=EventBus(),
    )
    launched = asyncio.Event()
    monkeypatch.setattr(queue, "_launch_claimed_task_run", lambda task, claim: launched.set())
    monkeypatch.setattr(queue, "recover_stale_tasks", AsyncMock())
    monkeypatch.setattr(queue, "recover_paused_tasks", AsyncMock())
    try:
        # SQLite current_timestamp has whole-second precision.
        due = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=2)
        async with factory() as session:
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Scheduled",
                status="ready",
                scheduled_for=due,
            )
            await session.commit()
        await queue.start()
        await asyncio.sleep(0.1)
        assert not launched.is_set()
        await asyncio.wait_for(launched.wait(), timeout=3)
    finally:
        queue._stop_event.set()
        queue._wake_event.set()
        if queue._drain_task is not None:
            await queue._drain_task
        await queue.stop()
        await engine.dispose()


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
@pytest.mark.parametrize("preserve_context", [False, True])
async def test_retry_failed_task_starts_new_attempt_and_clears_stale_result(
    tmp_path: object,
    preserve_context: bool,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            task = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Retry task",
                status="failed",
                workflow_state=WorkflowState(
                    status="failed",
                    current_step="execute",
                    loop_iterations={"execute": 3},
                ).model_dump(mode="json"),
                task_id="task-retry-history",
            )
            task.completed_at = datetime(2026, 1, 1, tzinfo=UTC)
            task.result_summary = "Original failure"
            task.result_data = {"error": "Original error"}
            await create_step_run(
                session,
                task_id=task.task_id,
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr-original",
                attempt=1,
                attempt_number=1,
                status="failed",
                completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            await session.commit()

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            event_bus=EventBus(),
        )
        original_claim: TaskExecutionClaim | None = None
        claim_existing = queue._execution_store.claim_existing

        async def capture_claim(task_id: str, *, statuses: set[str]) -> TaskExecutionClaim | None:
            nonlocal original_claim
            assert task_id == "task-retry-history"
            assert statuses == {"failed"}
            original_claim = await claim_existing(task_id, statuses=statuses)
            return original_claim

        queue._execution_store.claim_existing = capture_claim
        launched: list[TaskExecutionClaim] = []
        queue._launch_claimed_task_run = (  # type: ignore[method-assign]
            lambda task, claim: launched.append(claim)
        )

        retried = await queue.retry_failed_task(
            "task-retry-history", preserve_step_context=preserve_context
        )

        assert retried.attempt_number == (1 if preserve_context else 2)
        assert retried.workflow_state.resume_failed_step is preserve_context
        assert retried.status == TaskStatus.RUNNING
        assert len(launched) == 1
        assert launched[0].attempt_number == (1 if preserve_context else 2)
        assert original_claim is not None
        assert launched[0].task_lease is original_claim.task_lease
        async with factory() as session:
            persisted = await get_task(session, "task-retry-history")
            original_run = await get_step_run(session, "sr-original")
            assert persisted is not None
            assert persisted.attempt_number == (1 if preserve_context else 2)
            assert persisted.workflow_state["resume_failed_step"] is preserve_context
            assert persisted.completed_at is None
            assert persisted.result_summary is None
            assert persisted.result_data is None
            assert persisted.workflow_state["loop_iterations"] == {}
            assert original_run is not None
            assert original_run.status == "failed"
            assert original_run.attempt_number == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_available", "pending_receipt", "continuation_required"),
    [(False, False, False), (True, False, False), (True, True, False), (True, True, True)],
)
async def test_recovery_executes_failed_step_with_original_tool_evidence(
    tmp_path: object,
    history_available: bool,
    pending_receipt: bool,
    continuation_required: bool,
) -> None:
    from cognis.core.agent_loop import PauseWaiter
    from cognis.core.canonical_history import CanonicalHistoryUnavailable
    from cognis.core.runtime import ResolvedStepRuntime
    from cognis.core.task_execution import (
        TaskExecutionFence,
        bind_task_execution_fence,
        reset_task_execution_fence,
    )
    from cognis.core.task_tool_recovery import RecoveredTaskTools, recover_task_tools
    from cognis.core.workflow_engine import WorkflowEngine
    from cognis.models.agent import AgentDefinition
    from cognis.models.session import EventAppendResult, EventReadResult
    from cognis.models.workflow import CompletionConfig, StepOutput
    from cognis.store.models import Session
    from cognis.tools.registry import ToolRegistry

    db_engine, factory = await _bootstrap_db(tmp_path)
    source_events = [
        {
            "seq": 1,
            "type": "tool_call",
            "data": {"call_id": "create-ticket", "name": "create_ticket"},
        },
        {
            "seq": 2,
            "type": "tool_result",
            "data": {
                "call_id": "create-ticket",
                "name": "create_ticket",
                "content": "Ticket already created: TICKET-42",
            },
        },
    ]
    history: dict[str, list] = {"intaris-source": source_events}
    cache_events: dict[str, list] = {}

    async def read_events(session_id: str, **kwargs: object) -> EventReadResult:
        if not history_available:
            raise RuntimeError("Source event store unavailable")
        events = history.get(session_id, [])
        return EventReadResult(
            events=events,
            last_seq=len(events),
            has_more=False,
            first_available_seq=1 if events else None,
            history_gap=None,
        )

    async def record_events(*, session_id: str, events: list, **kwargs: object):
        target = history.setdefault(session_id, [])
        first = len(target) + 1
        target.extend(
            {"seq": first + i, "type": event.type, "data": event.data}
            for i, event in enumerate(events)
        )
        return EventAppendResult(ok=True, count=len(events), first_seq=first, last_seq=len(target))

    async def seed_events(session: object, events: list, seq: int) -> None:
        cache_events.setdefault(session.session_id, []).extend(events)

    async def append_events(session: object, events: list, result: object) -> None:
        cache_events.setdefault(session.session_id, []).extend(events)

    guardrails = SimpleNamespace(
        read_events=read_events,
        record_events=record_events,
    )
    fence_token = None
    try:
        async with factory() as db:
            conversation = await create_conversation(db, "user@test.com", "agent-1", "task_step")
            source = await create_session(
                db,
                conversation.conversation_id,
                "user@test.com",
                "agent-1",
                status="completed",
                intaris_session_id="intaris-source",
            )
            task = await create_task(
                db,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Recover",
                status="failed",
                workflow_state=WorkflowState(status="failed").model_dump(),
            )
            run = await create_step_run(
                db,
                task_id=task.task_id,
                step_name="execute",
                step_type="run",
                agent_id="agent-1",
                conversation_id=conversation.conversation_id,
                attempt_number=1,
            )
            run.session_id = source.session_id
            run.intaris_session_id = "intaris-source"
            run.status = "failed"
            if pending_receipt:
                for event in source_events:
                    event["data"]["turn_id"] = run.step_run_id
                    if event["type"] == "tool_result":
                        event["data"]["is_error"] = False
                    else:
                        event["data"]["arguments"] = {"title": "Existing ticket"}
                task.result_data = {
                    "execution_recovery": {
                        f"intaris-source:{run.step_run_id}": {
                            "session_id": "intaris-source",
                            "turn_id": run.step_run_id,
                            "attempt_number": 1,
                            "tool_calls": [
                                {
                                    "call_id": "create-ticket",
                                    "tool_name": "create_ticket",
                                    "canonical_turn_id": run.step_run_id,
                                }
                            ],
                        }
                    }
                }
            await db.commit()
            task_id = task.task_id

        async def create_root(**kwargs: object):
            async with factory() as db:
                target = await create_session(
                    db,
                    kwargs["conversation_id"],
                    kwargs["user_email"],
                    kwargs["agent_id"],
                    intaris_session_id="intaris-recovered",
                    previous_session_id=kwargs.get("previous_session_id"),
                )
                await db.commit()
                return target

        async def run_step(ctx: object, **kwargs: object) -> StepOutput | RecoveredTaskTools | None:
            assert ctx.is_retry
            if kwargs.get("settlement_only") and continuation_required:
                assert ctx.session.session_id == source.session_id
                return RecoveredTaskTools({}, set(), True)
            if kwargs.get("settlement_only") or kwargs.get("recovery_only"):
                assert ctx.session.session_id == source.session_id
                ctx.turn_id = ctx.step_run_id
                await recover_task_tools(
                    agent_loop,
                    ctx,
                    await fence.recovery_batches(),
                )
                return None
            assert not await fence.recovery_batches()
            assert ctx.session.session_id != source.session_id
            assert any(
                event.type == "tool_result"
                and event.data.get("content") == "Ticket already created: TICKET-42"
                for event in cache_events[ctx.session.session_id]
            )
            return StepOutput(summary="Continued with the existing ticket")

        agent_loop = SimpleNamespace(
            run_step=AsyncMock(side_effect=run_step),
            providers=SimpleNamespace(guardrails=guardrails),
            _session_factory=factory,
        )
        workflow_engine = WorkflowEngine(
            session_factory=factory,
            providers=SimpleNamespace(guardrails=guardrails, guardrail=guardrails, llm=None),
            agent_loop=agent_loop,
            step_evaluator=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            session_manager=SimpleNamespace(
                create_root_session=create_root,
                refresh_intaris_session_policy=AsyncMock(),
                mark_failed=AsyncMock(),
            ),
            event_bus=EventBus(),
            pause_waiter=PauseWaiter(),
        )
        workflow_engine._session_cache = SimpleNamespace(
            seed_events=seed_events,
            append_recorded_events=append_events,
            get_entry=lambda session_id: (
                SimpleNamespace(
                    events=cache_events[session_id],
                    last_event_seq=len(cache_events[session_id]),
                    initialized=True,
                    canonical_stale=False,
                )
                if session_id in cache_events
                else None
            ),
        )
        agent = AgentDefinition(
            agent_id="agent-1",
            owner_email="user@test.com",
            name="Agent",
            model="openai:gpt-4o",
        )
        workflow_engine._resolve_step_agents = AsyncMock(return_value=(agent, agent))
        workflow_engine._step_runtime_factory = AsyncMock(
            return_value=ResolvedStepRuntime(
                tool_registry=ToolRegistry(),
                executor_connection=None,
                cleanup=AsyncMock(),
                executor_environment=None,
                runtime_info={},
            )
        )
        workflow_engine._resolve_step_reasoning_effort = AsyncMock(return_value=None)
        workflow_engine._apply_step_runtime_overrides = AsyncMock()
        workflow_engine._persist_current_todos = AsyncMock()

        queue = TaskQueue(
            session_factory=factory,
            workflow_registry=WorkflowRegistry(factory),
            agent_registry=AgentRegistry(factory),
            workflow_engine=workflow_engine,
            event_bus=EventBus(),
        )
        launched: list[TaskExecutionClaim] = []
        queue._launch_claimed_task_run = lambda _task, claim: launched.append(claim)
        recovered = await queue.retry_failed_task(task_id, preserve_step_context=True)
        fence = TaskExecutionFence(
            queue._execution_store, launched[0], cancel_event=asyncio.Event()
        )
        fence_token = bind_task_execution_fence(fence)
        step = StepDefinition(
            name="execute",
            type="run",
            prompt="Continue",
            require_deliverable=False,
            completion=CompletionConfig(evaluate=False),
        )
        workflow = Workflow(workflow_id="wf:recovery", name="Recovery", steps=[step])
        if history_available:
            output, _ = await workflow_engine._execute_run_step(
                recovered, step, recovered.workflow_state, workflow
            )
            assert output.summary == "Continued with the existing ticket"
            async with factory() as db:
                continued_run = await db.get(StepRun, run.step_run_id)
                target = await db.get(Session, continued_run.session_id)
                assert target.previous_session_id == source.session_id
            if not pending_receipt:
                agent_loop.run_step.assert_awaited_once()
        else:
            with pytest.raises(CanonicalHistoryUnavailable):
                await workflow_engine._execute_run_step(
                    recovered, step, recovered.workflow_state, workflow
                )
            agent_loop.run_step.assert_not_awaited()
    finally:
        if fence_token is not None:
            reset_task_execution_fence(fence_token)
        await db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_pages", "expected"),
    [
        (
            [[]],
            "infrastructure_wait",
        ),
        (
            [
                [
                    {
                        "type": "tool_call",
                        "data": {"call_id": "call-1", "name": "bash"},
                    }
                ]
            ],
            "ambiguous_tool_execution",
        ),
        (
            [
                [{"_stalled": True}],
                [
                    {
                        "type": "tool_call",
                        "seq": 501,
                        "data": {"call_id": "call-unseen", "name": "bash"},
                    }
                ],
            ],
            "infrastructure_wait",
        ),
        (
            [
                [
                    {
                        "type": "tool_call",
                        "data": {"call_id": "call-1", "name": "bash"},
                    },
                    {
                        "type": "tool_result",
                        "data": {"call_id": "call-1", "name": "bash"},
                    },
                ]
            ],
            "recoverable_completed_tool_calls",
        ),
        (
            [
                [
                    {
                        "type": "tool_call",
                        "seq": 1,
                        "data": {"call_id": "call-1", "name": "read"},
                    },
                    {
                        "type": "tool_result",
                        "seq": 2,
                        "data": {"call_id": "call-1", "name": "read"},
                    },
                ],
                [
                    {
                        "type": "tool_call",
                        "seq": 501,
                        "data": {"call_id": "call-later", "name": "bash"},
                    }
                ],
            ],
            "ambiguous_tool_execution",
        ),
    ],
)
async def test_crash_recovery_classifies_tool_event_pairs(
    tmp_path: object,
    event_pages: list[list[dict[str, object]]],
    expected: str,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task-recovery",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Recovery",
                status="running",
            )
            await create_step_run(
                session,
                task_id="task-recovery",
                step_name="run",
                step_type="run",
                agent_id="agent-1",
                step_run_id="step-recovery",
                status="running",
            )
            step = await get_step_run(session, "step-recovery")
            assert step is not None
            step.intaris_session_id = "intaris-recovery"
            await session.commit()
            row = await get_task(session, "task-recovery")
            assert row is not None

        class _Guardrails:
            cursors: list[int] = []

            async def read_events(self, **kwargs: object) -> object:
                after_seq = int(kwargs.get("after_seq", 0))
                self.cursors.append(after_seq)
                page_index = 0 if after_seq == 0 else 1
                events = event_pages[page_index]
                stalled = any(event.get("_stalled") for event in events)
                return SimpleNamespace(
                    events=events,
                    has_more=page_index < len(event_pages) - 1,
                    last_seq=after_seq if stalled else 10_000,
                    missing_stream_fallback_used=not events,
                )

        guardrails = _Guardrails()
        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(_providers=SimpleNamespace(guardrails=guardrails)),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(),
        )
        classification, _ = await queue._classify_crash_recovery(  # noqa: SLF001
            _row_to_task_model(row)
        )
        assert classification == expected
        if len(event_pages) > 1 and not any(event.get("_stalled") for event in event_pages[0]):
            first_page_seqs = [
                int(event["seq"]) for event in event_pages[0] if isinstance(event.get("seq"), int)
            ]
            assert guardrails.cursors == [0, max(first_page_seqs)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_info", "persisted_output", "expected"),
    [
        (
            {
                "deterministic_step": True,
                "deterministic_substate": "rendering",
            },
            None,
            "safe_before_tool_dispatch",
        ),
        (
            {
                "deterministic_step": True,
                "deterministic_substate": "executing",
                "tool_read_only": True,
                "tool_name": "read",
            },
            None,
            "retry_read_only_deterministic_tool",
        ),
        (
            {
                "deterministic_step": True,
                "deterministic_substate": "executing",
                "tool_read_only": False,
                "tool_name": "mutate",
            },
            None,
            "ambiguous_tool_execution",
        ),
        (
            {
                "deterministic_step": True,
                "deterministic_substate": "executing",
                "tool_name": "unclassified",
            },
            None,
            "ambiguous_tool_execution",
        ),
        (
            {
                "deterministic_step": True,
                "deterministic_substate": "persisted",
                "terminal_status": "approved",
            },
            {"summary": "Persisted", "outputs": {}, "metadata": {}, "claims": []},
            "recoverable_persisted_deterministic_output",
        ),
    ],
)
async def test_crash_recovery_uses_deterministic_substate_without_intaris_session(
    tmp_path: object,
    runtime_info: dict[str, object],
    persisted_output: dict[str, object] | None,
    expected: str,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task-deterministic-recovery",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Deterministic recovery",
                status="running",
            )
            await create_step_run(
                session,
                task_id="task-deterministic-recovery",
                step_name="fetch",
                step_type="tool_call",
                agent_id="agent-1",
                step_run_id="step-deterministic-recovery",
                status="running",
                runtime_info=runtime_info,
            )
            if persisted_output is not None:
                await update_step_run(
                    session,
                    "step-deterministic-recovery",
                    output=persisted_output,
                )
            await session.commit()
            row = await get_task(session, "task-deterministic-recovery")
            assert row is not None

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(_providers=SimpleNamespace()),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(),
        )
        classification, details = await queue._classify_crash_recovery(  # noqa: SLF001
            _row_to_task_model(row)
        )

        assert classification == expected
        assert details["step_run_id"] == "step-deterministic-recovery"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_tasks_for_agent_includes_creator_agent_tasks(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            session.add(
                Agent(
                    agent_id="agent-2",
                    owner_email="user@test.com",
                    name="Agent 2",
                )
            )
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                created_by_agent_id="agent-2",
                title="Delegated task",
                status="draft",
            )
            await session.commit()

        async with factory() as session:
            tasks = await list_tasks_for_agent(session, "agent-2")

        assert [task.title for task in tasks] == ["Delegated task"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerun_task_resumes_paused_task_in_place(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Paused task",
                status="paused",
            )
            await session.commit()
            task_id = row.task_id

        async def _publish(*_args: object, **_kwargs: object) -> None:
            return None

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(
                _pause_waiter=SimpleNamespace(find_pending=lambda **_kwargs: None)
            ),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(publish=_publish),
        )

        async def _has_capacity(*, agent_id: str | None = None) -> bool:
            assert agent_id == "agent-1"
            return True

        launches: list[str] = []

        def _launch(task: TaskModel) -> None:
            launches.append(task.task_id)

        queue._has_capacity = _has_capacity  # type: ignore[method-assign]
        queue._launch_task_run = _launch  # type: ignore[method-assign]

        result = await queue.rerun_task(task_id)

        assert result == TaskRerunResult(
            source_task_id=task_id,
            task=result.task,
            created_new=False,
        )
        assert result.task.task_id == task_id
        assert result.task.status == TaskStatus.RUNNING
        assert launches == [task_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerun_task_clones_terminal_task_with_dependencies(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            conversation = await create_conversation(
                session,
                user_email="user@test.com",
                agent_id="agent-1",
                context_type="web",
                conversation_id="conv-1",
            )
            source_session = await create_session(
                session,
                conversation_id=conversation.conversation_id,
                user_email="user@test.com",
                agent_id="agent-1",
            )
            await update_conversation_active_session(
                session,
                conversation.conversation_id,
                source_session.session_id,
            )
            dependency = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Dependency",
                status="completed",
                task_id="task_dependency",
            )
            original = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Original task",
                description="Run the same workflow again",
                expected_output="Ship the final report",
                status="failed",
                priority=42,
                source_type="chat",
                source_ref="conv-1",
                delivery_mode="specific_conversation",
                delivery_target="conv-2",
                completion_mode_family="direct",
                allow_silent_completion=True,
                workflow_id="wf-1",
                workspace_root="/tmp/workspace",
                working_directory="/tmp/workspace/subdir",
                task_id="task_original",
            )
            await add_task_dependency(session, original.task_id, dependency.task_id, required=False)
            await session.commit()

        async def _publish(*_args: object, **_kwargs: object) -> None:
            return None

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(
                _pause_waiter=SimpleNamespace(find_pending=lambda **_kwargs: None)
            ),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(publish=_publish),
        )

        result = await queue.rerun_task("task_original")

        assert result.created_new is True
        assert result.source_task_id == "task_original"
        assert result.task.task_id != "task_original"
        assert result.task.status == TaskStatus.QUEUED

        async with factory() as session:
            cloned = await get_task(session, result.task.task_id)
            dependencies = await get_task_dependencies(session, result.task.task_id)
            assert cloned is not None
            assert cloned.title == "Original task"
            assert cloned.description == "Run the same workflow again"
            assert cloned.expected_output == "Ship the final report"
            assert cloned.priority == 42
            assert cloned.agent_id == "agent-1"
            assert cloned.source_type == "chat"
            assert cloned.source_ref == "conv-1"
            assert cloned.delivery_mode == "specific_conversation"
            assert cloned.delivery_target == "conv-2"
            assert cloned.completion_mode_family == "direct"
            assert cloned.allow_silent_completion is True
            assert cloned.workflow_id == "wf-1"
            assert cloned.workspace_root == "/tmp/workspace"
            assert cloned.working_directory == "/tmp/workspace/subdir"
            assert cloned.status == "ready"
            assert len(dependencies) == 1
            assert dependencies[0].depends_on == "task_dependency"
            assert dependencies[0].required is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerun_task_rejects_active_statuses(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            row = await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Running task",
                status="running",
                task_id="task_running_rerun",
            )
            await session.commit()

        async def _publish(*_args: object, **_kwargs: object) -> None:
            return None

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(
                _pause_waiter=SimpleNamespace(find_pending=lambda **_kwargs: None)
            ),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(publish=_publish),
        )

        with pytest.raises(ValueError, match="Cannot rerun task in 'running' status"):
            await queue.rerun_task(row.task_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_omitted_workflow_uses_general_task_despite_attached_skill(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)

    try:
        async with factory() as session:
            agent = await session.get(Agent, "agent-1")
            assert agent is not None
            agent.skills = {"items": [{"skill_id": "skill_release", "enabled": True}]}
            skill = await create_skill(
                session,
                skill_id="skill_release",
                name="Release Helper",
                description="Release workflow material.",
                instructions="Gather notes and publish the release.",
                owner_email="user@test.com",
            )
            version = await create_skill_version(
                session,
                skill_id=skill.skill_id,
                version_number=1,
                content_hash="a" * 64,
                instructions="Gather notes and publish the release.",
                steps=[
                    {
                        "name": "gather_notes",
                        "type": "run",
                        "prompt": "Gather the release notes.",
                        "require_deliverable": False,
                    },
                    {
                        "name": "publish_release",
                        "type": "run",
                        "prompt": "Publish the release.",
                        "require_deliverable": True,
                    },
                ],
                decomposition_source_hash="b" * 64,
            )
            await set_current_version(session, skill.skill_id, version.version_id)
            await session.commit()

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=AsyncMock(),
            workflow_registry=WorkflowRegistry(factory),
            event_bus=EventBus(),
            agent_registry=AgentRegistry(factory),
        )

        task = await queue.submit(
            title="Ship release",
            description="Use the attached release skill.",
            created_by="user@test.com",
            agent_id="agent-1",
        )
        assert task.workflow_id == "system:general-task"
        async with factory() as session:
            persisted_task = await get_task(session, task.task_id)
            assert persisted_task is not None
            assert persisted_task.workflow_id == "system:general-task"
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
async def test_queue_refetches_task_after_claiming_detached_session_row(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    launched: list[TaskModel] = []
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task-detached-claim",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Claimed task",
                status="ready",
            )
            await session.commit()

        async def _publish(*_args: object, **_kwargs: object) -> None:
            return None

        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=SimpleNamespace(),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(publish=_publish),
        )
        queue._launch_claimed_task_run = (  # type: ignore[method-assign]
            lambda task, _claim: launched.append(task)
        )

        await queue._try_pick_and_run()  # noqa: SLF001

        assert [task.task_id for task in launched] == ["task-detached-claim"]
        assert launched[0].status == TaskStatus.RUNNING
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


# ---------------------------------------------------------------------------
# recover_paused_tasks — behavior for each pause type
# ---------------------------------------------------------------------------


class _FakePauseWaiter:
    def __init__(self) -> None:
        self.registered: dict[str, SimpleNamespace] = {}
        self.resolved: list[tuple[str, SimpleNamespace]] = []

    def register(self, pending: SimpleNamespace) -> None:
        self.registered[pending.pause_id] = pending

    def get(self, pause_id: str) -> SimpleNamespace | None:
        return self.registered.get(pause_id)

    def resolve(self, pause_id: str, resolution: SimpleNamespace) -> bool:
        self.resolved.append((pause_id, resolution))
        return True


class _FakeNotificationService:
    def __init__(self, rows: dict[str, SimpleNamespace]) -> None:
        self._rows = rows

    async def get(self, notification_id: str) -> SimpleNamespace | None:
        return self._rows.get(notification_id)


async def _build_queue_for_recovery(
    tmp_path: object, notification_rows: dict[str, SimpleNamespace] | None = None
) -> tuple[TaskQueue, _FakePauseWaiter, object]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cognis.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = create_session_factory(engine)

    fake_waiter = _FakePauseWaiter()
    fake_engine = SimpleNamespace(
        _pause_waiter=fake_waiter,
        _notification_service=_FakeNotificationService(notification_rows or {}),
    )
    queue = TaskQueue(
        session_factory=factory,
        workflow_engine=fake_engine,
        workflow_registry=SimpleNamespace(),
        event_bus=SimpleNamespace(publish=lambda *_a, **_k: None),
    )
    queue.launch_calls = []  # type: ignore[attr-defined]

    def _record_launch(task: object) -> None:
        queue.launch_calls.append(task)  # type: ignore[attr-defined]

    queue._launch_task_run = _record_launch  # type: ignore[method-assign]

    return queue, fake_waiter, engine


@pytest.mark.asyncio
async def test_recover_paused_tasks_step_input_with_persisted_response(
    tmp_path: object,
) -> None:
    queue, waiter, engine = await _build_queue_for_recovery(tmp_path)
    try:
        async with queue._session_factory() as session:  # noqa: SLF001
            session.add(User(email="user@test.com", name="Test", role="admin"))
            await session.flush()
            session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="A"))
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Paused step input",
                status="paused",
                task_id="task_step_input",
            )
            state = {
                "current_step_index": 0,
                "pending_pause_type": "step_input",
                "pending_pause_payload": {
                    "pause_id": "input_1",
                    "step_name": "step-a",
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Ready?",
                            "options": [],
                            "multiple": False,
                            "allow_custom": True,
                            "required": True,
                        }
                    ],
                    "answers": [
                        {
                            "question_id": "q1",
                            "selected_option_ids": [],
                            "custom_answer": "yes",
                        }
                    ],
                    "mode": "structured",
                },
            }
            await update_task_workflow_state(session, "task_step_input", state)
            await session.commit()

        recovered = await queue.recover_paused_tasks()
        assert recovered == ["task_step_input"]
        # Persisted response → relaunch without registering a new waiter.
        assert len(queue.launch_calls) == 1  # type: ignore[attr-defined]
        assert waiter.registered == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recover_paused_tasks_step_input_awaiting_user(tmp_path: object) -> None:
    queue, waiter, engine = await _build_queue_for_recovery(tmp_path)
    try:
        async with queue._session_factory() as session:  # noqa: SLF001
            session.add(User(email="user@test.com", name="Test", role="admin"))
            await session.flush()
            session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="A"))
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Paused awaiting user",
                status="paused",
                task_id="task_awaiting",
            )
            state = {
                "current_step_index": 0,
                "pending_pause_type": "step_input",
                "pending_pause_payload": {
                    "pause_id": "input_2",
                    "step_name": "step-a",
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Ready?",
                            "options": [],
                            "multiple": False,
                            "allow_custom": True,
                            "required": True,
                        }
                    ],
                },
            }
            await update_task_workflow_state(session, "task_awaiting", state)
            await session.commit()

        recovered = await queue.recover_paused_tasks()
        assert recovered == ["task_awaiting"]
        # No persisted response → takeover relaunches the authoritative DB wait.
        assert [task.task_id for task in queue.launch_calls] == ["task_awaiting"]  # type: ignore[attr-defined]
        assert "input_2" in waiter.registered
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deliver_terminal_task_failure_uses_workflow_delivery_path(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    delivered: list[str] = []

    class _WorkflowEngine:
        _pause_waiter = SimpleNamespace(find_pending=lambda **_kwargs: None)

        async def _deliver_task_result(self, task: TaskModel) -> None:
            delivered.append(task.task_id)

    async def _publish(*_args: object, **_kwargs: object) -> None:
        return None

    try:
        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=_WorkflowEngine(),
            workflow_registry=SimpleNamespace(),
            event_bus=SimpleNamespace(publish=_publish),
        )
        task = TaskModel(
            task_id="task_fail",
            title="Failure",
            created_by="user@test.com",
            agent_id="agent-1",
            status=TaskStatus.FAILED,
        )

        await queue._deliver_terminal_task_failure(task)  # type: ignore[attr-defined]

        assert delivered == ["task_fail"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recover_paused_tasks_credential_replays_resolved_notification(
    tmp_path: object,
) -> None:
    resolved_row = SimpleNamespace(
        status="resolved",
        resolution={
            "decision": "continue",
            "state": "resolved",
            "credential_id": "cred-abc",
            "credential_label": "My token",
            "credential_kind": "api_key",
        },
    )
    queue, waiter, engine = await _build_queue_for_recovery(
        tmp_path, notification_rows={"cred_1": resolved_row}
    )
    try:
        async with queue._session_factory() as session:  # noqa: SLF001
            session.add(User(email="user@test.com", name="Test", role="admin"))
            await session.flush()
            session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="A"))
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Paused credential",
                status="paused",
                task_id="task_cred",
            )
            state = {
                "current_step_index": 0,
                "pending_pause_type": "credential_request",
                "pending_pause_payload": {
                    "pause_id": "cred_1",
                    "step_name": "step-a",
                    "message": "Need token",
                },
            }
            await update_task_workflow_state(session, "task_cred", state)
            await session.commit()

        recovered = await queue.recover_paused_tasks()
        assert recovered == ["task_cred"]
        # Resolution replayed: waiter registered and resolved, task launched.
        assert "cred_1" in waiter.registered
        assert waiter.resolved and waiter.resolved[0][0] == "cred_1"
        assert len(queue.launch_calls) == 1  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recover_paused_tasks_credential_pending_stays_paused(tmp_path: object) -> None:
    queue, waiter, engine = await _build_queue_for_recovery(tmp_path, notification_rows={})
    try:
        async with queue._session_factory() as session:  # noqa: SLF001
            session.add(User(email="user@test.com", name="Test", role="admin"))
            await session.flush()
            session.add(Agent(agent_id="agent-1", owner_email="user@test.com", name="A"))
            await create_task(
                session,
                created_by="user@test.com",
                agent_id="agent-1",
                title="Paused credential pending",
                status="paused",
                task_id="task_cred_pending",
            )
            state = {
                "current_step_index": 0,
                "pending_pause_type": "credential_request",
                "pending_pause_payload": {
                    "pause_id": "cred_2",
                    "step_name": "step-a",
                    "message": "Need token",
                },
            }
            await update_task_workflow_state(session, "task_cred_pending", state)
            await session.commit()

        recovered = await queue.recover_paused_tasks()
        assert recovered == ["task_cred_pending"]
        # Not resolved → waiter registered and takeover relaunches the DB wait.
        assert "cred_2" in waiter.registered
        assert waiter.resolved == []
        assert [task.task_id for task in queue.launch_calls] == ["task_cred_pending"]  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_request_revision_reopens_selected_step_and_supersedes_later_runs(
    tmp_path: object,
) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    workflow = Workflow(
        workflow_id="wf-revision",
        name="Revision",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="build", type="run"),
            StepDefinition(name="review", type="run"),
        ],
    )
    state = WorkflowState(
        current_step_index=3,
        status="completed",
        step_outputs={
            "plan": {"summary": "plan"},
            "build": {"summary": "build"},
            "review": {"summary": "review"},
        },
        effective_workflow_definition=workflow.model_dump(mode="json"),
    )
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task-revision",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Revision",
                status="completed",
                workflow_id=workflow.workflow_id,
                workflow_state=state.model_dump(mode="json"),
            )
            for name in ("plan", "build", "review"):
                await create_step_run(
                    session,
                    task_id="task-revision",
                    step_name=name,
                    step_type="run",
                    agent_id="agent-1",
                    step_run_id=f"sr-{name}",
                    status="approved",
                    attempt_number=1,
                )
            await session.commit()

        workflow_engine = SimpleNamespace(
            _notification_service=None,
            _pause_waiter=SimpleNamespace(find_pending=lambda **_kwargs: None),
        )
        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=workflow_engine,
            workflow_registry=SimpleNamespace(),
            event_bus=EventBus(),
            max_active_steps_global=1,
            max_active_steps_per_agent=1,
        )
        launched: list[object] = []
        queue._launch_claimed_task_run = lambda task, claim: launched.extend([task, claim])  # type: ignore[method-assign]  # noqa: SLF001
        old_owner = await queue._execution_store.claim_existing(  # noqa: SLF001
            "task-revision",
            statuses={"completed"},
        )
        assert old_owner is not None

        with pytest.raises(ValueError, match="target step is required"):
            await queue.request_revision(
                "task-revision",
                target_step=None,
                instruction="Correct the implementation",
                expected_attempt=1,
            )
        async with factory() as session:
            unchanged = await get_task(session, "task-revision")
            assert unchanged is not None
            assert unchanged.attempt_number == 1
            assert unchanged.status == "completed"
            assert all(
                row.status == "approved"
                for row in await session.scalars(
                    select(StepRun).where(StepRun.task_id == "task-revision")
                )
            )

        revised = await queue.request_revision(
            "task-revision",
            target_step="build",
            instruction="Correct the implementation",
            expected_attempt=1,
        )

        assert revised.attempt_number == 2
        assert revised.status == TaskStatus.RUNNING
        assert revised.relaunched is False
        assert revised.target_step == "build"
        assert revised.superseded_count == 2
        assert revised.task.workflow_state is not None
        assert revised.task.workflow_state.current_step_index == 1
        assert set(revised.task.workflow_state.step_outputs) == {"plan"}
        async with factory() as session:
            rows = {
                row.step_name: row
                for row in await session.scalars(
                    select(StepRun).where(StepRun.task_id == "task-revision")
                )
            }
        assert rows["plan"].status == "approved"
        assert rows["build"].status == "superseded"
        assert rows["review"].status == "superseded"
        assert launched == []
        await queue._execution_store.release(old_owner)  # noqa: SLF001
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_queue_cancellation_is_durable_and_idempotent(tmp_path: object) -> None:
    engine, factory = await _bootstrap_db(tmp_path)
    try:
        async with factory() as session:
            await create_task(
                session,
                task_id="task-cancel",
                created_by="user@test.com",
                agent_id="agent-1",
                title="Cancel",
                status="running",
            )
            await create_step_run(
                session,
                task_id="task-cancel",
                step_name="run",
                step_type="run",
                agent_id="agent-1",
                step_run_id="sr-cancel",
                status="evaluating",
            )
            await session.commit()

        workflow_engine = SimpleNamespace(
            _notification_service=None,
            _pause_waiter=SimpleNamespace(find_pending=lambda **_kwargs: None),
            deliver_task_outcome=AsyncMock(),
        )
        queue = TaskQueue(
            session_factory=factory,
            workflow_engine=workflow_engine,
            workflow_registry=SimpleNamespace(),
            event_bus=EventBus(),
            controller_owner_id="controller-b",
        )

        first = await queue.cancel_task("task-cancel", expected_attempt=1)
        second = await queue.cancel_task("task-cancel", expected_attempt=1)

        assert first.status == TaskStatus.CANCELLED
        assert second.status == TaskStatus.CANCELLED
        workflow_engine.deliver_task_outcome.assert_awaited_once()
        async with factory() as session:
            task = await get_task(session, "task-cancel")
            step = await get_step_run(session, "sr-cancel")
        assert task is not None and task.status == "cancelled"
        assert step is not None and step.status == "cancelled"
    finally:
        await engine.dispose()
