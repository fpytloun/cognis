from __future__ import annotations

from cognis.api.models import PendingPauseResponse, WorkflowRunResponse
from cognis.models.task import WorkflowState


def test_workflow_run_response_keeps_workflow_fields() -> None:
    response = WorkflowRunResponse(
        task_id="task-1",
        workflow_id="wf-1",
        workflow_state=WorkflowState(),
        current_step_name="plan",
        pending_pause=PendingPauseResponse(pause_id="pause-1", pause_type="step_input"),
    )

    assert response.workflow_id == "wf-1"
    assert response.current_step_name == "plan"
    assert response.pending_pause is not None
