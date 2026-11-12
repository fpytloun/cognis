"""Task output freshness through the real API and persisted workflow metadata."""

from __future__ import annotations

import pytest

from cognis.core.task_output_presentation import resolve_task_outputs
from cognis.models.task import TaskModel
from cognis.models.workflow import (
    OnRejectConfig,
    StepDefinition,
    Workflow,
    WorkflowState,
    pin_effective_workflow,
)
from cognis.store.models import DeliverableRow, StepRun, Task
from cognis.store.queries import (
    create_agent,
    create_deliverable,
    create_step_run,
    create_task,
    create_user,
)
from tests.unit.test_api_stage7 import _auth_headers, _create_test_client


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_terminal_task_output_is_partial(status):
    task = TaskModel(
        task_id="partial-task",
        created_by="owner@example.com",
        agent_id="agent",
        title="Brief",
        status=status,
        result_data={"final_deliverable_id": "partial-doc"},
    )
    run = StepRun(
        step_run_id="partial-run",
        task_id=task.task_id,
        step_name="Publish",
        step_type="run",
        agent_id="agent",
        status=status,
        attempt=1,
        attempt_number=1,
        deliverable_id="partial-doc",
    )
    document = DeliverableRow(
        deliverable_id="partial-doc",
        step_run_id=run.step_run_id,
        format="markdown",
        version=1,
        revision=1,
        title="Verified so far",
    )
    output = resolve_task_outputs(task, [run], [document]).output
    assert output.deliverable_id == "partial-doc"
    assert output.freshness.model_dump() == {
        "state": "stale",
        "replacement_status": status,
        "label": f"Partial output — task {status}",
    }


@pytest.mark.parametrize("replacement_status", ["running", "paused", "failed", "cancelled"])
@pytest.mark.parametrize("human_revision", [False, True])
def test_pending_replacement_is_stale_until_write(
    monkeypatch, tmp_path, replacement_status, human_revision
):
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def seed():
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                )
                await create_agent(
                    session,
                    agent_id="output-agent",
                    owner_email="user@example.com",
                    name="Output agent",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="output-agent",
                    title="Revision",
                    status="running",
                )
                workflow = Workflow(
                    workflow_id="output-workflow",
                    name="Output",
                    steps=[StepDefinition(name="publish", type="run", require_deliverable=True)],
                )
                task.workflow_state = pin_effective_workflow(WorkflowState(), workflow).model_dump(
                    mode="json"
                )
                run = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_name="publish",
                    step_type="run",
                    agent_id="output-agent",
                    status="running",
                )
                document = await create_deliverable(
                    session,
                    step_run_id=run.step_run_id,
                    content="Original document",
                    format="markdown",
                    artifact_store=app.state.artifact_store,
                )
                run.deliverable_id = document.deliverable_id
                await session.commit()
                return task.task_id, run.step_run_id, document.deliverable_id

        task_id, run_id, original_id = client.portal.call(seed)
        headers = _auth_headers(app, email="user@example.com")

        def detail():
            response = client.get(f"/api/v1/tasks/{task_id}/summary", headers=headers)
            assert response.status_code == 200, response.text
            return response.json()["output_presentation"]

        assert detail()["freshness"]["state"] == "current"

        async def restart():
            async with app.state.session_factory() as session:
                task = await session.get(Task, task_id)
                run = await session.get(StepRun, run_id)
                task.status = replacement_status
                if human_revision:
                    task.attempt_number = 2
                    task.result_data = None
                    run.status = "superseded"
                else:
                    run.attempt = 2
                    run.status = replacement_status
                    run.deliverable_id = None
                    document = await session.get(DeliverableRow, original_id)
                    document.status = "rejected"
                await session.commit()

        client.portal.call(restart)
        pending = detail()
        assert pending["deliverable_id"] == original_id
        assert pending["freshness"]["state"] == "stale"
        assert pending["freshness"]["replacement_status"] == replacement_status
        summary = client.get(f"/api/v1/tasks/{task_id}/summary", headers=headers).json()
        assert summary["step_runs"] == []
        assert summary["deliverable_freshness"][original_id]["state"] == "stale"
        assert (
            summary["deliverable_freshness"][original_id]["replacement_status"]
            == replacement_status
        )
        assert (
            "in progress" in pending["freshness"]["label"]
            if replacement_status == "running"
            else replacement_status in pending["freshness"]["label"]
        )
        projected = client.get(f"/api/v1/step-runs/{run_id}", headers=headers).json()
        assert projected["deliverables"][0]["task_freshness"] == pending["freshness"]
        detail_response = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert detail_response.status_code == 200, detail_response.text
        assert detail_response.json()["output_presentation"] == pending
        summaries = client.get(f"/api/v1/tasks/{task_id}/steps/summary", headers=headers)
        assert summaries.status_code == 200, summaries.text
        assert (
            summaries.json()["items"][0]["deliverable_freshness"][original_id]
            == pending["freshness"]
        )
        original = client.get(
            f"/api/v1/step-runs/{run_id}/deliverables/{original_id}", headers=headers
        )
        assert original.status_code == 200
        assert original.json()["content"] == "Original document"
        assert original.json()["task_freshness"] == pending["freshness"]

        async def write():
            async with app.state.session_factory() as session:
                task = await session.get(Task, task_id)
                task.status = "running"
                run = await session.get(StepRun, run_id)
                if human_revision:
                    run = await create_step_run(
                        session,
                        task_id=task_id,
                        step_name="publish",
                        step_type="run",
                        agent_id="output-agent",
                        status="running",
                        attempt_number=2,
                    )
                else:
                    run.status = "running"
                document = await create_deliverable(
                    session,
                    step_run_id=run.step_run_id,
                    content="Replacement document",
                    format="markdown",
                    artifact_store=app.state.artifact_store,
                )
                run.deliverable_id = document.deliverable_id
                await session.commit()
                return document.deliverable_id

        replacement_id = client.portal.call(write)
        current = detail()
        assert current["deliverable_id"] == replacement_id
        assert current["freshness"]["state"] == "current"
        freshness = client.get(f"/api/v1/tasks/{task_id}/summary", headers=headers).json()[
            "deliverable_freshness"
        ]
        assert freshness[original_id]["state"] == "historical"
        assert freshness[replacement_id]["state"] == "current"
        old = client.get(f"/api/v1/step-runs/{run_id}/deliverables/{original_id}", headers=headers)
        assert old.json()["task_freshness"]["state"] == "historical"


def test_nonlinear_output_ownership_is_unknown(monkeypatch, tmp_path):
    with _create_test_client(monkeypatch, tmp_path) as client:
        app = client.app

        async def seed():
            async with app.state.session_factory() as session:
                await create_user(
                    session,
                    email="user@example.com",
                    name="Owner",
                    password_hash=app.state.password_hasher.hash("password123"),
                )
                await create_agent(
                    session,
                    agent_id="branch-agent",
                    owner_email="user@example.com",
                    name="Branch agent",
                    status="active",
                )
                task = await create_task(
                    session,
                    created_by="user@example.com",
                    agent_id="branch-agent",
                    title="Branching output",
                    status="running",
                )
                workflow = Workflow(
                    workflow_id="branch-workflow",
                    name="Branching",
                    steps=[
                        StepDefinition(name="publish", type="run", require_deliverable=True),
                        StepDefinition(
                            name="review", type="gate", on_reject=OnRejectConfig(target="publish")
                        ),
                    ],
                )
                task.workflow_state = pin_effective_workflow(WorkflowState(), workflow).model_dump(
                    mode="json"
                )
                run = await create_step_run(
                    session,
                    task_id=task.task_id,
                    step_name="publish",
                    step_type="run",
                    agent_id="branch-agent",
                    status="running",
                )
                document = await create_deliverable(
                    session,
                    step_run_id=run.step_run_id,
                    content="Not necessarily the task result",
                    format="markdown",
                    artifact_store=app.state.artifact_store,
                )
                run.deliverable_id = document.deliverable_id
                await session.commit()
                return task.task_id

        response = client.get(
            f"/api/v1/tasks/{client.portal.call(seed)}/summary",
            headers=_auth_headers(app, email="user@example.com"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["output_presentation"]["deliverable_id"] is None
        assert response.json()["output_presentation"]["freshness"]["state"] == "unknown"
