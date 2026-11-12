"""Durable gate recovery through the real persistence and decision boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.core.agent_loop import PauseWaiter, StepInterrupted
from cognis.core.events import EventBus
from cognis.core.management import resolve_task_pause_action, task_pending_pause_response
from cognis.core.notifications import NotificationService
from cognis.core.task_execution import StaleTaskExecutionOwner
from cognis.core.task_queue import TaskQueue, _row_to_task_model
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.models.workflow import (
    GateConfig,
    GateOption,
    StepDefinition,
    StepOutcome,
    StepOutput,
    Workflow,
    WorkflowState,
)
from cognis.store.models import NotificationRow, StepRun, Task
from cognis.store.queries import create_conversation, create_step_run, create_task
from tests.unit.test_task_queue import _bootstrap_db
from tests.unit.test_workflow_engine_runtime import _build_engine


@pytest.fixture
async def gate_stack(tmp_path):
    database, factory = await _bootstrap_db(tmp_path)
    workflow = Workflow(
        workflow_id="wf:gate-recovery",
        name="Recovery",
        steps=[
            StepDefinition(
                name="execute",
                type="run",
                prompt="Do work",
                outcome_routes=[{"status": "failed", "action": "gate"}],
            )
        ],
    )
    output = StepOutput(
        summary="Evidence collected",
        content="Preserve me",
        outcome=StepOutcome(status="failed", reason="Required source unavailable"),
    )
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    state = WorkflowState(
        status="paused",
        current_step_status="paused",
        step_outputs={"execute": output.model_dump(mode="json")},
        pending_pause_type="gate",
        pending_pause_payload={
            "pause_id": "gate_recovery",
            "step_name": "execute_outcome_gate",
            "origin_step": "execute",
            "task_attempt": 1,
            "expires_at": deadline.isoformat(),
            "options": [
                {"label": "Retry", "action": "revise(execute)"},
                {"label": "Continue", "action": "continue"},
                {"label": "Cancel", "action": "cancel"},
            ],
        },
    )
    async with factory() as db:
        conversation = await create_conversation(db, "user@test.com", "agent-1", "web")
        row = await create_task(
            db,
            created_by="user@test.com",
            agent_id="agent-1",
            title="Gate recovery",
            status="paused",
            workflow_state=state.model_dump(mode="json"),
            delivery_mode="silent",
        )
        task_id = row.task_id
        step = await create_step_run(
            db,
            task_id=task_id,
            step_name="execute",
            step_type="run",
            agent_id="agent-1",
            status="approved",
        )
        step.output = output.model_dump(mode="json")
        step_id = step.step_run_id
        db.add(
            NotificationRow(
                notification_id="gate_recovery",
                notification_type="gate",
                user_email="user@test.com",
                conversation_id=conversation.conversation_id,
                task_id=task_id,
                step_name="execute_outcome_gate",
                status="pending",
                expires_at=deadline,
                payload={**state.pending_pause_payload, "timeout_action": "fail"},
            )
        )
        await db.commit()
    waiter = PauseWaiter()
    service = NotificationService(
        session_factory=factory,
        pause_waiter=waiter,
        event_bus=EventBus(),
        providers=SimpleNamespace(),
    )
    engine = _build_engine()
    engine._session_factory = factory
    engine._notification_service = service
    engine._pause_waiter = waiter
    engine._create_gate_session = AsyncMock(return_value=None)
    engine._cleanup_step_sessions = AsyncMock()

    async def load():
        async with factory() as db:
            return _row_to_task_model(await db.get(Task, task_id))

    yield SimpleNamespace(
        factory=factory,
        task_id=task_id,
        step_id=step_id,
        load=load,
        engine=engine,
        service=service,
        workflow=workflow,
        waiter=waiter,
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_persisted_unsupported_gate_action_pauses_without_advancing(gate_stack):
    stack = gate_stack
    state = WorkflowState(status="running", current_step_index=0)
    async with stack.factory() as db:
        row = await db.get(Task, stack.task_id)
        row.status = "running"
        row.workflow_state = state.model_dump(mode="json")
        await db.commit()

    workflow = Workflow(
        workflow_id="wf:legacy-gate",
        name="Legacy gate",
        steps=[
            StepDefinition(
                name="approve",
                type="gate",
                gate=GateConfig(
                    message="Approve?",
                    options=[GateOption(label="Reject", action="reject")],
                ),
            ),
            StepDefinition(name="after", type="run"),
        ],
    )
    result = await stack.engine.execute_workflow(await stack.load(), workflow)
    assert result.status == "paused"
    assert result.workflow_state.current_step_index == 0
    assert "reconcile" in result.result_summary.lower()
    stored = await stack.load()
    assert stored.status == "paused"
    assert stored.workflow_state.current_step_index == 0


@pytest.mark.asyncio
async def test_derived_gate_name_collision_cannot_authorize_forward_revision(gate_stack):
    stack = gate_stack
    stack.workflow.steps.extend(
        [
            StepDefinition(name="after", type="run"),
            StepDefinition(
                name="execute_outcome_gate",
                type="gate",
                gate=GateConfig(message="Later approval"),
            ),
        ]
    )
    async with stack.factory() as db:
        notification = await db.get(NotificationRow, "gate_recovery")
        notification.status = "resolved"
        notification.resolution = {"decision": "revise(after)"}
        row = await db.get(Task, stack.task_id)
        state = dict(row.workflow_state)
        payload = dict(state["pending_pause_payload"])
        payload["gate_definition"] = {
            "message": "Review",
            "options": [{"label": "Revise later", "action": "revise(after)"}],
        }
        state["pending_pause_payload"] = payload
        row.workflow_state = state
        row.status = "running"
        await db.commit()

    result = await stack.engine.execute_workflow(await stack.load(), stack.workflow)
    assert result.status == "paused"
    assert result.workflow_state.current_step_index == 0
    assert "reconcile" in result.result_summary.lower()
    assert (await stack.load()).status == "paused"


@pytest.mark.asyncio
async def test_saved_unsupported_gate_decision_requires_reconciliation(gate_stack):
    stack = gate_stack
    async with stack.factory() as db:
        notification = await db.get(NotificationRow, "gate_recovery")
        notification.status = "resolved"
        notification.resolution = {"decision": "reject"}
        row = await db.get(Task, stack.task_id)
        row.status = "running"
        await db.commit()

    result = await stack.engine.execute_workflow(await stack.load(), stack.workflow)
    assert result.status == "paused"
    assert result.workflow_state.current_step_index == 0
    assert "reconcile" in result.result_summary.lower()
    stored = await stack.load()
    assert stored.status == "paused"
    assert stored.workflow_state.current_step_index == 0


@pytest.mark.asyncio
async def test_saved_derived_gate_with_unsupported_option_requires_reconciliation(gate_stack):
    stack = gate_stack
    async with stack.factory() as db:
        row = await db.get(Task, stack.task_id)
        state = dict(row.workflow_state)
        payload = dict(state["pending_pause_payload"])
        payload["gate_definition"] = {
            "message": "Review",
            "options": [
                {"label": "Reject", "action": "reject"},
                {"label": "Continue", "action": "continue"},
            ],
        }
        state["pending_pause_payload"] = payload
        row.workflow_state = state
        await db.commit()
    assert await stack.service.resolve("gate_recovery", "continue", user_email="user@test.com")
    async with stack.factory() as db:
        row = await db.get(Task, stack.task_id)
        row.status = "running"
        await db.commit()

    result = await stack.engine.execute_workflow(await stack.load(), stack.workflow)
    assert result.status == "paused"
    assert result.workflow_state.current_step_index == 0
    assert "reconcile" in result.result_summary.lower()
    stored = await stack.load()
    assert stored.status == "paused"
    assert stored.workflow_state.current_step_index == 0


@pytest.mark.asyncio
async def test_recovered_outcome_gate_never_reenters_completed_step(gate_stack):
    stack = gate_stack
    task = await stack.load()
    stack.engine._execute_run_step = AsyncMock(
        side_effect=AssertionError("Recovery must not rerun the completed step")
    )
    assert await stack.service.resolve(
        "gate_recovery", "continue", {"note": "Keep evidence"}, user_email="user@test.com"
    )
    result = await stack.engine.execute_workflow(task, stack.workflow)
    assert result.status == "completed"
    stack.engine._execute_run_step.assert_not_called()
    stored = await stack.load()
    assert stored.workflow_state.pending_pause_type is None
    assert stored.workflow_state.current_step_index == 1
    async with stack.factory() as db:
        step = await db.get(StepRun, stack.step_id)
        assert step.status == "approved"
        assert step.output["content"] == "Preserve me"


@pytest.mark.asyncio
async def test_gate_decision_consumption_is_committed_with_destination(gate_stack, monkeypatch):
    stack = gate_stack
    assert await stack.service.resolve("gate_recovery", "continue", user_email="user@test.com")
    task = await stack.load()
    original_persist = stack.engine._persist_workflow_state

    async def crash_before_commit(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(stack.engine, "_persist_workflow_state", crash_before_commit)
    with pytest.raises(asyncio.CancelledError):
        await stack.engine._resume_derived_gate(
            task, stack.workflow.steps[0], task.workflow_state, stack.workflow
        )
    stored = await stack.load()
    assert stored.workflow_state.pending_pause_type == "gate"
    assert stored.workflow_state.current_step_index == 0
    monkeypatch.setattr(stack.engine, "_persist_workflow_state", original_persist)
    assert await stack.engine._resume_derived_gate(
        stored, stack.workflow.steps[0], stored.workflow_state, stack.workflow
    )
    committed = await stack.load()
    assert committed.status == "running"
    assert committed.workflow_state.pending_pause_type is None
    assert committed.workflow_state.current_step_index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["fail", "continue", "cancel"])
async def test_gate_expiry_resolves_configured_action_not_blanket_cancel(gate_stack, action):
    stack = gate_stack
    async with stack.factory() as db:
        notification = await db.get(NotificationRow, "gate_recovery")
        notification.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        notification.payload = {**notification.payload, "timeout_action": action}
        await db.commit()
    assert not await stack.service.resolve(
        "gate_recovery", "revise(execute)", user_email="user@test.com"
    )
    assert await stack.service.mark_orphaned("gate_recovery", reason="expired")
    resolved = await stack.service.get("gate_recovery")
    assert resolved.status == "resolved"
    assert resolved.resolution["decision"] == action
    assert resolved.resolution["reason"] == "timeout"


@pytest.mark.asyncio
async def test_operator_winner_is_not_overwritten_by_later_timeout(gate_stack):
    stack = gate_stack
    assert await stack.service.resolve(
        "gate_recovery", "continue", {"note": "Approved"}, user_email="user@test.com"
    )
    assert not await stack.service.resolve("gate_recovery", "fail", {"reason": "timeout"})
    assert (await stack.service.get("gate_recovery")).resolution["decision"] == "continue"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["cancel", "new_attempt", "new_gate"])
async def test_stale_gate_cannot_resolve_after_task_state_changes(gate_stack, mutation):
    stack = gate_stack
    async with stack.factory() as db:
        row = await db.get(Task, stack.task_id)
        if mutation == "cancel":
            row.status = "cancelled"
        elif mutation == "new_attempt":
            row.attempt_number += 1
        else:
            state = dict(row.workflow_state)
            state["pending_pause_payload"] = {
                **state["pending_pause_payload"],
                "pause_id": "gate_later",
            }
            row.workflow_state = state
        await db.commit()
    assert not await stack.service.resolve("gate_recovery", "continue", user_email="user@test.com")
    assert (await stack.service.get("gate_recovery")).status == "pending"


@pytest.mark.asyncio
async def test_gate_ingress_uses_exact_pause_and_does_not_rewrite_workflow(gate_stack):
    stack = gate_stack
    task = await stack.load()
    snapshot = task.workflow_state.model_dump(mode="json")
    await resolve_task_pause_action(
        task=task,
        requested_action="retry",
        note="Collect again",
        pause_waiter=stack.waiter,
        notification_service=stack.service,
        task_queue=None,
        session_factory=stack.factory,
        user_email="user@test.com",
        notification_id="gate_recovery",
    )
    assert (await stack.load()).workflow_state.model_dump(mode="json") == snapshot
    assert (await stack.service.get("gate_recovery")).resolution["note"] == "Collect again"


@pytest.mark.asyncio
async def test_cancellation_atomically_clears_gate_and_preserves_evidence(gate_stack):
    stack = gate_stack
    queue = TaskQueue(
        session_factory=stack.factory,
        workflow_engine=stack.engine,
        workflow_registry=WorkflowRegistry(stack.factory),
        event_bus=EventBus(),
    )
    await queue.cancel_task(stack.task_id)
    stored = await stack.load()
    assert stored.status == stored.workflow_state.status == "cancelled"
    assert stored.workflow_state.pending_pause_type is None
    assert task_pending_pause_response(stack.waiter, stored) is None
    async with stack.factory() as db:
        assert (await db.get(StepRun, stack.step_id)).output["content"] == "Preserve me"


@pytest.mark.asyncio
async def test_workflow_version_conflict_cannot_write_status(gate_stack):
    stack = gate_stack
    task = await stack.load()
    async with stack.factory() as db:
        row = await db.get(Task, stack.task_id)
        row.workflow_state = {**row.workflow_state, "version": 20}
        await db.commit()
    task.status = "running"
    task.workflow_state.status = "running"
    task.workflow_state.pending_pause_type = None
    with pytest.raises(StaleTaskExecutionOwner):
        await stack.engine._persist_workflow_state(task, sync_status=True)
    assert (await stack.load()).status == "paused"


@pytest.mark.asyncio
async def test_legacy_missing_notification_and_deadline_stays_paused(gate_stack):
    stack = gate_stack
    task = await stack.load()
    task.workflow_state.pending_pause_payload.pop("expires_at")
    stack.engine._notification_service = SimpleNamespace(get=AsyncMock(return_value=None))
    with pytest.raises(StepInterrupted):
        await stack.engine._resume_derived_gate(
            task, stack.workflow.steps[0], task.workflow_state, stack.workflow
        )
    assert (await stack.load()).workflow_state.pending_pause_type == "gate"


@pytest.mark.asyncio
async def test_terminal_task_projection_hides_legacy_phantom_gate(gate_stack):
    task = await gate_stack.load()
    task.status = "cancelled"
    assert task_pending_pause_response(gate_stack.waiter, task) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("submission_id", [None, "accepted-submission"])
async def test_recovery_finalizes_claimed_operator_winner_after_expiry(gate_stack, submission_id):
    stack = gate_stack
    async with stack.factory() as db:
        row = await db.get(NotificationRow, "gate_recovery")
        row.status = "resolving"
        row.resolution = {
            "decision": "continue",
            "state": "resolving",
            "claim_token": "interrupted-claim",
            "note": "Operator's accepted instruction",
        }
        if submission_id is not None:
            row.resolution["submission_id"] = submission_id
        row.resolved_at = datetime.now(UTC) - timedelta(minutes=2)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    assert not await stack.service.resolve("gate_recovery", "fail", {"reason": "timeout"})
    assert not await stack.service.resolve(
        "gate_recovery",
        "continue",
        {
            "note": "Different instruction",
            **({"submission_id": submission_id} if submission_id is not None else {}),
        },
        user_email="user@test.com",
    )
    result = await stack.service.wait_for_resolution("gate_recovery", timeout=1.0)
    assert result.decision == "continue"
    assert result.data["note"] == "Operator's accepted instruction"
    assert (await stack.service.get("gate_recovery")).status == "resolved"
    assert await stack.engine._resume_derived_gate(
        (task := await stack.load()), stack.workflow.steps[0], task.workflow_state, stack.workflow
    )
    assert (await stack.load()).workflow_state.current_step_index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_name", [None, "another_gate"])
@pytest.mark.parametrize("autonomous", [False, True])
async def test_ambiguous_explicit_gate_never_becomes_a_new_gate(gate_stack, saved_name, autonomous):
    stack = gate_stack
    task = await stack.load()
    task.workflow_state.pending_pause_payload["step_name"] = saved_name
    if autonomous:
        task.interaction_mode_override = "none"
    gate = StepDefinition(
        name="review",
        type="gate",
        gate=GateConfig(
            message="Approval",
            conditions=[{"expression": "{{ false }}", "else": "continue"}],
        ),
    )
    with pytest.raises(StepInterrupted):
        await stack.engine._handle_gate_step(task, gate, task.workflow_state, stack.workflow)
    assert task.workflow_state.pending_pause_type == "gate"
    assert task.workflow_state.current_step_index == 0


@pytest.mark.asyncio
async def test_two_controllers_admit_only_one_distinct_gate_decision(gate_stack):
    stack = gate_stack
    other = NotificationService(
        session_factory=stack.factory,
        pause_waiter=PauseWaiter(),
        event_bus=EventBus(),
        providers=SimpleNamespace(),
    )
    decisions = ["continue", "revise(execute)"]
    results = await asyncio.gather(
        stack.service.resolve("gate_recovery", decisions[0], user_email="user@test.com"),
        other.resolve("gate_recovery", decisions[1], user_email="user@test.com"),
    )
    assert results.count(True) == 1
    assert (await stack.service.get("gate_recovery")).resolution["decision"] == decisions[
        results.index(True)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,expected",
    [
        ("fail", "failed"),
        ("cancel", "cancelled"),
        ("continue", "completed"),
    ],
)
async def test_recovery_consumes_durable_timeout_without_running_step(gate_stack, action, expected):
    stack = gate_stack
    async with stack.factory() as db:
        row = await db.get(NotificationRow, "gate_recovery")
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        row.payload = {**row.payload, "timeout_action": action}
        await db.commit()
    assert await stack.service.mark_orphaned("gate_recovery", reason="expired")
    stack.engine._execute_run_step = AsyncMock(
        side_effect=AssertionError("Timeout recovery must not execute the step")
    )
    result = await stack.engine.execute_workflow(await stack.load(), stack.workflow)
    assert result.status == expected
    stored = await stack.load()
    assert stored.workflow_state.pending_pause_type is None
    assert stored.workflow_state.status == expected
    stack.engine._execute_run_step.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_uses_original_notification_deadline(gate_stack):
    stack = gate_stack
    async with stack.factory() as db:
        row = await db.get(NotificationRow, "gate_recovery")
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    task = await stack.load()
    # The workflow has the older/future projection, the notification is canonical.
    assert datetime.fromisoformat(
        task.workflow_state.pending_pause_payload["expires_at"]
    ) > datetime.now(UTC)
    wait = AsyncMock(wraps=stack.service.wait_for_resolution)
    stack.service.wait_for_resolution = wait
    assert await stack.engine._resume_derived_gate(
        task, stack.workflow.steps[0], task.workflow_state, stack.workflow
    )
    assert task.workflow_state.status == "failed"
    assert wait.call_args_list[0].kwargs["timeout"] <= 0.01
    assert (await stack.service.get("gate_recovery")).resolution["decision"] == "fail"


@pytest.mark.asyncio
async def test_explicit_gate_recovery_does_not_reevaluate_conditions(gate_stack):
    stack = gate_stack
    task = await stack.load()
    step = StepDefinition(
        name="execute_outcome_gate",
        type="gate",
        gate=GateConfig(
            message="Review saved gate",
            conditions=[{"expression": "{{ false }}", "else": "continue"}],
            options=[GateOption(label="Continue", action="continue")],
        ),
    )
    # Already decided retry must survive a now-false gate condition.
    assert await stack.service.resolve(
        "gate_recovery", "revise(execute)", user_email="user@test.com"
    )
    result = await stack.engine._handle_gate_step(
        task, step, task.workflow_state, stack.workflow, derived=True
    )
    assert result == "revise(execute)"
    assert (await stack.load()).workflow_state.pending_pause_type == "gate"


@pytest.mark.asyncio
async def test_exhaustion_recovery_preserves_approved_row_until_retry_is_accepted(gate_stack):
    stack = gate_stack
    async with stack.factory() as db:
        row = await db.get(Task, stack.task_id)
        state = dict(row.workflow_state)
        state["pending_pause_payload"] = {
            **state["pending_pause_payload"],
            "step_name": "execute_exhausted",
        }
        row.workflow_state = state
        notification = await db.get(NotificationRow, "gate_recovery")
        notification.step_name = "execute_exhausted"
        await db.commit()
    assert await stack.service.resolve("gate_recovery", "continue", user_email="user@test.com")
    task = await stack.load()
    assert await stack.engine._resume_derived_gate(
        task, stack.workflow.steps[0], task.workflow_state, stack.workflow
    )
    assert task.workflow_state.current_step_index == 1
    async with stack.factory() as db:
        assert (await db.get(StepRun, stack.step_id)).output["content"] == "Preserve me"
