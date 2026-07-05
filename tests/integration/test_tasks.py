"""Task and workflow integration tests."""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    LiveStack,
    create_test_agent,
    live_create_agent,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_task_lifecycle_draft_submit_complete(live_stack: LiveStack, run_id: str) -> None:
    """Create a draft task, submit it, and verify it progresses through the workflow."""
    agent_id = f"task-agent-{run_id}"
    live_create_agent(live_stack, agent_id)

    create_response = live_stack.post(
        "/api/v1/tasks",
        json={
            "agent_id": agent_id,
            "title": "Integration test task",
            "description": "Say hello and nothing else.",
            "workflow_id": "system:direct",
            "priority": 5,
            "delivery_mode": "silent",
            "status": "draft",
        },
    )
    assert create_response.status_code == 200
    task = create_response.json()
    task_id = task["task_id"]
    assert task["status"] == "draft"

    submit = live_stack.post(f"/api/v1/tasks/{task_id}/submit")
    assert submit.status_code == 200

    deadline = time.monotonic() + 120
    final_status = "queued"
    while time.monotonic() < deadline:
        detail = live_stack.get(f"/api/v1/tasks/{task_id}")
        assert detail.status_code == 200
        final_status = detail.json()["status"]
        if final_status in ("completed", "failed", "cancelled", "paused"):
            break
        time.sleep(3)

    assert final_status in ("completed", "failed", "paused"), (
        f"Task did not reach terminal state within 120s, got: {final_status}"
    )


@pytest.mark.integration
@pytest.mark.live_server
def test_task_batch_submit(live_stack: LiveStack, run_id: str) -> None:
    """Batch-submit multiple draft tasks."""
    agent_id = f"batch-agent-{run_id}"
    live_create_agent(live_stack, agent_id)

    task_ids = []
    for i in range(2):
        r = live_stack.post(
            "/api/v1/tasks",
            json={
                "agent_id": agent_id,
                "title": f"Batch task {i}",
                "description": "Say one word.",
                "workflow_id": "system:direct",
                "priority": 1,
                "delivery_mode": "silent",
                "status": "draft",
            },
        )
        assert r.status_code == 200
        task_ids.append(r.json()["task_id"])

    batch = live_stack.post("/api/v1/tasks/batch-submit", json={"task_ids": task_ids})
    assert batch.status_code == 200
    assert batch.json()["succeeded"] == 2


@pytest.mark.integration
def test_task_dependency_management(stack: IntegrationStack, agent_id: str) -> None:
    """Add and remove task dependencies."""
    create_test_agent(stack, agent_id)

    tasks = []
    for i in range(2):
        r = stack.client.post(
            "/api/v1/tasks",
            headers=stack.admin_headers(),
            json={
                "agent_id": agent_id,
                "title": f"Dep task {i}",
                "description": "Test dependency",
                "delivery_mode": "silent",
                "status": "draft",
            },
        )
        assert r.status_code == 200
        tasks.append(r.json())

    dep = stack.client.post(
        f"/api/v1/tasks/{tasks[1]['task_id']}/dependencies",
        headers=stack.admin_headers(),
        json={"depends_on": tasks[0]["task_id"], "required": True},
    )
    assert dep.status_code == 200

    detail = stack.client.get(
        f"/api/v1/tasks/{tasks[1]['task_id']}",
        headers=stack.admin_headers(),
    )
    assert detail.status_code == 200
    deps = detail.json()["dependencies"]
    assert len(deps) == 1
    assert deps[0]["depends_on"] == tasks[0]["task_id"]

    remove = stack.client.delete(
        f"/api/v1/tasks/{tasks[1]['task_id']}/dependencies/{tasks[0]['task_id']}",
        headers=stack.admin_headers(),
    )
    assert remove.status_code == 200
