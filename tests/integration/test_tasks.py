"""Task and workflow integration tests.

Exercises: task CRUD, submission, workflow execution via task queue.
"""

from __future__ import annotations

import time

import pytest

from tests.integration.conftest import (
    IntegrationStack,
    create_test_agent,
)


@pytest.mark.integration
@pytest.mark.live_server
def test_task_lifecycle_draft_submit_complete(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """Create a draft task, submit it, and verify it progresses through the workflow."""
    create_test_agent(stack, agent_id)

    # Create a draft task
    create_response = stack.client.post(
        "/api/v1/tasks",
        headers=stack.admin_headers(),
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
    assert create_response.status_code == 200, f"Task creation failed: {create_response.text}"
    task = create_response.json()
    task_id = task["task_id"]
    assert task["status"] == "draft"

    # Submit the task
    submit_response = stack.client.post(
        f"/api/v1/tasks/{task_id}/submit",
        headers=stack.admin_headers(),
    )
    assert submit_response.status_code == 200, f"Task submit failed: {submit_response.text}"

    # Poll until the task is no longer queued/running (completed, failed, or still running)
    deadline = time.monotonic() + 90
    final_status = "queued"
    while time.monotonic() < deadline:
        detail_response = stack.client.get(
            f"/api/v1/tasks/{task_id}",
            headers=stack.admin_headers(),
        )
        assert detail_response.status_code == 200
        detail = detail_response.json()
        final_status = detail["status"]
        if final_status in ("completed", "failed", "cancelled"):
            break
        time.sleep(2)

    assert final_status in ("completed", "failed"), (
        f"Task did not reach a terminal state within 90s, got: {final_status}"
    )


@pytest.mark.integration
@pytest.mark.live_server
def test_task_batch_submit(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """Batch-submit multiple draft tasks."""
    create_test_agent(stack, agent_id)

    task_ids = []
    for i in range(2):
        response = stack.client.post(
            "/api/v1/tasks",
            headers=stack.admin_headers(),
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
        assert response.status_code == 200
        task_ids.append(response.json()["task_id"])

    batch_response = stack.client.post(
        "/api/v1/tasks/batch-submit",
        headers=stack.admin_headers(),
        json={"task_ids": task_ids},
    )
    assert batch_response.status_code == 200
    batch_result = batch_response.json()
    assert batch_result["succeeded"] == 2


@pytest.mark.integration
def test_task_dependency_management(
    stack: IntegrationStack,
    agent_id: str,
) -> None:
    """Add and remove task dependencies."""
    create_test_agent(stack, agent_id)

    # Create two draft tasks
    tasks = []
    for i in range(2):
        response = stack.client.post(
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
        assert response.status_code == 200
        tasks.append(response.json())

    # Add dependency: task[1] depends on task[0]
    dep_response = stack.client.post(
        f"/api/v1/tasks/{tasks[1]['task_id']}/dependencies",
        headers=stack.admin_headers(),
        json={"depends_on": tasks[0]["task_id"], "required": True},
    )
    assert dep_response.status_code == 200

    # Verify dependency exists
    detail = stack.client.get(
        f"/api/v1/tasks/{tasks[1]['task_id']}",
        headers=stack.admin_headers(),
    )
    assert detail.status_code == 200
    deps = detail.json()["dependencies"]
    assert len(deps) == 1
    assert deps[0]["depends_on"] == tasks[0]["task_id"]

    # Remove the dependency
    remove_response = stack.client.delete(
        f"/api/v1/tasks/{tasks[1]['task_id']}/dependencies/{tasks[0]['task_id']}",
        headers=stack.admin_headers(),
    )
    assert remove_response.status_code == 200
