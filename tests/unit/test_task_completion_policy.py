"""Outcome-policy selection shared by owner delivery and project recipients."""

from __future__ import annotations

import pytest

from cognis.core.task_outcomes import task_outcome_completion_mode
from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import CompletionDeliveryPolicy, CompletionModeFamily, WorkflowState


@pytest.mark.parametrize("family", ["auto", "default", "direct"])
@pytest.mark.parametrize(
    "status",
    [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED],
)
def test_policy_resolves_success_and_non_success(family: str, status: TaskStatus) -> None:
    task = TaskModel(
        task_id="task-policy-selection",
        title="Policy selection",
        created_by="owner@example.com",
        agent_id="agent-policy-selection",
        status=status,
        completion_delivery=CompletionDeliveryPolicy(completion_mode_family=family),
    )
    expected = (
        "direct"
        if family == "direct" or (family == "auto" and status == TaskStatus.COMPLETED)
        else "default"
    )
    assert task_outcome_completion_mode(task) == expected

    task.applied_completion_mode = "silent"
    assert task_outcome_completion_mode(task) == "silent"


def test_default_is_hybrid_and_saved_agent_policy_keeps_its_meaning() -> None:
    assert CompletionDeliveryPolicy().completion_mode_family == CompletionModeFamily.DEFAULT
    assert CompletionDeliveryPolicy().mode_for_outcome(succeeded=True) == "direct"
    saved = CompletionDeliveryPolicy.model_validate({"completion_mode_family": "default"})
    assert saved.completion_mode_family == CompletionModeFamily.AGENT
    assert saved.mode_for_outcome(succeeded=True) == "default"
    assert saved.model_dump(mode="json")["completion_mode_family"] == "default"


@pytest.mark.parametrize("family", ["auto", "default"])
@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_finish_instructions_do_not_turn_hybrid_success_into_agent_handling(
    family: str, status: TaskStatus
) -> None:
    task = TaskModel(
        task_id="task-instruction-policy",
        title="Instruction policy",
        created_by="owner@example.com",
        agent_id="agent-policy-selection",
        status=status,
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family=family, on_finish_instructions="Inspect and report failures."
        ),
    )
    engine = object.__new__(WorkflowEngine)
    expected = "direct" if family == "auto" and status == TaskStatus.COMPLETED else "default"
    assert engine._resolve_applied_completion(task, WorkflowState()) == (expected, None)
