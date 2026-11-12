"""Task revision and legacy gate behavior on SQLite and PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from cognis.core.events import EventBus
from cognis.core.task_queue import TaskQueue, _row_to_task_model
from cognis.models.workflow import GateConfig, GateOption, StepDefinition, Workflow, WorkflowState
from cognis.store.models import StepRun, Task
from cognis.store.queries import create_step_run, create_task
from tests.integration.test_task_delegation_recovery import database as database
from tests.unit.test_workflow_engine_runtime import _build_engine

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_revision_requires_target_before_durable_attempt_and_supersedes_suffix(database):
    task_id = uuid4().hex
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
        status="completed",
        current_step_index=3,
        step_outputs={"plan": {}, "build": {}, "review": {}},
        effective_workflow_definition=workflow.model_dump(mode="json"),
    )
    async with database() as db:
        await create_task(
            db,
            task_id=task_id,
            title="Revision",
            created_by="user@example.com",
            agent_id="primary",
            workflow_id=workflow.workflow_id,
            workflow_state=state.model_dump(mode="json"),
            status="completed",
        )
        for name in ("plan", "build", "review"):
            await create_step_run(
                db,
                task_id=task_id,
                step_name=name,
                step_type="run",
                agent_id="primary",
                status="approved",
                attempt_number=1,
            )
        await db.commit()
    queue = TaskQueue(
        session_factory=database,
        workflow_engine=SimpleNamespace(
            _notification_service=None,
            _pause_waiter=SimpleNamespace(find_pending=lambda **kwargs: None),
        ),
        workflow_registry=SimpleNamespace(),
        event_bus=EventBus(),
        max_active_steps_global=1,
        max_active_steps_per_agent=1,
    )
    queue._launch_claimed_task_run = lambda task, claim: None
    with pytest.raises(ValueError, match="target step is required"):
        await queue.request_revision(task_id, target_step=None, instruction="Revise")
    async with database() as db:
        row = await db.get(Task, task_id)
        runs = list(await db.scalars(select(StepRun).where(StepRun.task_id == task_id)))
        assert row.attempt_number == 1
        assert row.status == "completed"
        assert {run.status for run in runs} == {"approved"}
    result = await queue.request_revision(task_id, target_step="build", instruction="Revise")
    assert result.attempt_number == 2
    assert result.target_step == "build"
    assert result.superseded_count == 2
    async with database() as db:
        row = await db.get(Task, task_id)
        runs = {
            run.step_name: run.status
            for run in await db.scalars(select(StepRun).where(StepRun.task_id == task_id))
        }
        assert row.attempt_number == 2
        assert row.workflow_state["current_step_index"] == 1
        assert runs == {"plan": "approved", "build": "superseded", "review": "superseded"}


@pytest.mark.asyncio
async def test_legacy_unsupported_gate_action_pauses_without_advancing(database):
    task_id = uuid4().hex
    state = WorkflowState(status="running", current_step_index=0)
    async with database() as db:
        await create_task(
            db,
            task_id=task_id,
            title="Legacy gate",
            created_by="user@example.com",
            agent_id="primary",
            workflow_state=state.model_dump(mode="json"),
            status="running",
            delivery_mode="silent",
        )
        await db.commit()
    workflow = Workflow(
        workflow_id="wf-legacy-gate",
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
    engine = _build_engine()
    engine._session_factory = database
    async with database() as db:
        task = _row_to_task_model(await db.get(Task, task_id))
    result = await engine.execute_workflow(task, workflow)
    assert result.status == "paused"
    assert result.workflow_state.current_step_index == 0
    assert "reconcile" in result.result_summary.lower()
    async with database() as db:
        row = await db.get(Task, task_id)
        assert row.status == "paused"
        assert row.workflow_state["current_step_index"] == 0
