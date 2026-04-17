"""Tests for the workflow registry."""

from __future__ import annotations

import pytest

from cognis.core.workflow_registry import (
    CREATIVE_WORKFLOW,
    DIRECT_WORKFLOW,
    GENERAL_TASK_WORKFLOW,
    RESEARCH_WORKFLOW,
    SOFTWARE_DEVELOPMENT_WORKFLOW,
    SYSTEM_WORKFLOWS,
    _validate_workflow,
)
from cognis.models.workflow import (
    OnRejectConfig,
    OutcomeRoute,
    StepDefinition,
    Workflow,
)


def test_system_workflows_are_registered() -> None:
    assert "system:direct" in SYSTEM_WORKFLOWS
    assert "system:general-task" in SYSTEM_WORKFLOWS
    assert "system:research" in SYSTEM_WORKFLOWS
    assert "system:software-development" in SYSTEM_WORKFLOWS
    assert "system:creative" in SYSTEM_WORKFLOWS


def test_direct_workflow_has_single_step_no_evaluation() -> None:
    w = DIRECT_WORKFLOW
    assert len(w.steps) == 1
    assert w.steps[0].name == "execute"
    assert w.steps[0].completion is not None
    assert w.steps[0].completion.evaluate is False


def test_general_task_workflow_has_single_step_with_evaluation() -> None:
    w = GENERAL_TASK_WORKFLOW
    assert len(w.steps) == 1
    assert w.steps[0].name == "execute"
    assert w.steps[0].reasoning_effort == "low"
    assert w.steps[0].completion is not None
    assert w.steps[0].completion.evaluate is True
    assert "smallest correct change" in w.steps[0].prompt
    assert w.steps[0].outcome_routes == [OutcomeRoute(status="failed", action="gate")]


def test_research_and_creative_workflows_gate_failed_outcomes() -> None:
    for step in RESEARCH_WORKFLOW.steps:
        assert step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]

    assert CREATIVE_WORKFLOW.steps[0].outcome_routes == [
        OutcomeRoute(status="failed", action="gate")
    ]


def test_software_development_workflow_uses_implement_specialist() -> None:
    implement_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "implement"
    )
    update_docs_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "update_docs"
    )

    assert implement_step.agent_override == "system:implement"
    assert implement_step.reasoning_effort == "medium"
    assert implement_step.input is not None
    assert implement_step.input.type == "summary"
    assert update_docs_step.agent_override == "system:implement"
    assert update_docs_step.input is not None
    assert update_docs_step.input.type == "summary"
    assert "no documentation updates are needed" in update_docs_step.prompt


def test_software_development_review_steps_use_outcome_routes() -> None:
    architect_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "architect_review"
    )
    code_review_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "code_review"
    )
    commit_step = next(
        step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == "commit"
    )

    assert architect_step.outcome_routes == [
        OutcomeRoute(
            status="rejected",
            action="revise(plan)",
            max_loop_iterations=3,
            on_exhausted="gate",
        ),
        OutcomeRoute(status="failed", action="gate"),
    ]
    assert architect_step.input is not None
    assert architect_step.input.type == "full"
    assert "do not block on nitpicks" in architect_step.prompt
    assert (
        "plan is sound and ready, complete the step normally with success" in architect_step.prompt
    )
    assert "Put the outcome only in step_complete" in architect_step.prompt
    assert code_review_step.outcome_routes == [
        OutcomeRoute(
            status="rejected",
            action="revise(implement)",
            max_loop_iterations=3,
            on_exhausted="gate",
        ),
        OutcomeRoute(status="failed", action="gate"),
    ]
    assert code_review_step.input is not None
    assert code_review_step.input.type == "summary"
    assert (
        "changes are acceptable, complete the step normally with success" in code_review_step.prompt
    )
    assert "Put the outcome only in step_complete" in code_review_step.prompt
    assert commit_step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]

    for step_name in ("plan", "implement", "update_docs", "remember"):
        step = next(step for step in SOFTWARE_DEVELOPMENT_WORKFLOW.steps if step.name == step_name)
        assert step.outcome_routes == [OutcomeRoute(status="failed", action="gate")]


def test_validate_workflow_accepts_valid_definition() -> None:
    workflow = Workflow(
        workflow_id="test:valid",
        name="Valid",
        steps=[
            StepDefinition(name="plan", type="run", prompt="Plan"),
            StepDefinition(name="implement", type="run", prompt="Implement", input=["plan"]),
        ],
    )
    # Should not raise
    _validate_workflow(workflow)


def test_validate_workflow_rejects_duplicate_step_names() -> None:
    workflow = Workflow(
        workflow_id="test:dup",
        name="Duplicate",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(name="plan", type="run"),
        ],
    )
    with pytest.raises(ValueError, match="Duplicate step name"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unknown_input_reference() -> None:
    workflow = Workflow(
        workflow_id="test:bad-input",
        name="Bad Input",
        steps=[
            StepDefinition(name="implement", type="run", input=["nonexistent"]),
        ],
    )
    with pytest.raises(ValueError, match="unknown/later input"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_forward_on_reject_target() -> None:
    workflow = Workflow(
        workflow_id="test:forward-reject",
        name="Forward Reject",
        steps=[
            StepDefinition(
                name="plan",
                type="run",
                on_reject=OnRejectConfig(target="implement"),
            ),
            StepDefinition(name="implement", type="run"),
        ],
    )
    with pytest.raises(ValueError, match="must reference an earlier step"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unknown_on_reject_target() -> None:
    workflow = Workflow(
        workflow_id="test:unknown-reject",
        name="Unknown Reject",
        steps=[
            StepDefinition(
                name="plan",
                type="run",
                on_reject=OnRejectConfig(target="nonexistent"),
            ),
        ],
    )
    with pytest.raises(ValueError, match="unknown step"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_gate_without_config() -> None:
    workflow = Workflow(
        workflow_id="test:gate-no-config",
        name="Gate No Config",
        steps=[
            StepDefinition(name="approve", type="gate"),
        ],
    )
    with pytest.raises(ValueError, match="must have gate configuration"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unknown_outcome_route_target() -> None:
    workflow = Workflow(
        workflow_id="test:bad-outcome-route",
        name="Bad Outcome Route",
        steps=[
            StepDefinition(
                name="review",
                type="run",
                outcome_routes=[OutcomeRoute(status="rejected", action="revise(plan)")],
            ),
        ],
    )
    with pytest.raises(ValueError, match="outcome route references unknown step"):
        _validate_workflow(workflow)


def test_validate_workflow_rejects_unsupported_outcome_route_action() -> None:
    workflow = Workflow(
        workflow_id="test:bad-outcome-action",
        name="Bad Outcome Action",
        steps=[
            StepDefinition(name="plan", type="run"),
            StepDefinition(
                name="review",
                type="run",
                outcome_routes=[OutcomeRoute(status="rejected", action="plan")],
            ),
        ],
    )
    with pytest.raises(ValueError, match="unsupported outcome route action"):
        _validate_workflow(workflow)


def test_validate_all_system_workflows() -> None:
    """Validate that all bundled system workflows pass validation."""
    for wf in SYSTEM_WORKFLOWS.values():
        _validate_workflow(wf)
