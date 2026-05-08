"""Tests for the workflow engine state machine."""

from __future__ import annotations

from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import (
    CompletionConfig,
    GateConfig,
    GateOption,
    InteractionMode,
    OnRejectConfig,
    StepDefinition,
    StepEvaluation,
    StepOutput,
    Workflow,
    WorkflowDefaults,
    WorkflowState,
)

# These tests validate the domain model state transitions.
# Full integration tests with the workflow engine are in integration/.


def test_task_model_default_status() -> None:
    task = TaskModel(
        task_id="t1",
        title="Test",
        created_by="user@test.com",
        agent_id="agent-1",
    )
    assert task.status == TaskStatus.DRAFT


def test_workflow_state_default() -> None:
    state = WorkflowState()
    assert state.current_step_index == 0
    assert state.step_outputs == {}
    assert state.loop_iterations == {}
    assert state.status == "running"


def test_workflow_state_ignores_unknown_retry_reason() -> None:
    state = WorkflowState.model_validate({"last_retry_reason": "legacy_reason"})

    assert state.last_retry_reason is None


def test_workflow_state_step_advance() -> None:
    state = WorkflowState()
    state.current_step_index += 1
    assert state.current_step_index == 1


def test_workflow_state_stores_step_outputs() -> None:
    state = WorkflowState()
    output = StepOutput(summary="Plan created", outputs={"plan": "test"}, claims=["Created plan"])
    state.step_outputs["plan"] = output.model_dump(mode="json")

    assert "plan" in state.step_outputs
    recovered = StepOutput.model_validate(state.step_outputs["plan"])
    assert recovered.summary == "Plan created"


def test_workflow_state_tracks_loop_iterations() -> None:
    state = WorkflowState()
    loop_key = "plan->review"
    state.loop_iterations[loop_key] = state.loop_iterations.get(loop_key, 0) + 1
    assert state.loop_iterations[loop_key] == 1
    state.loop_iterations[loop_key] += 1
    assert state.loop_iterations[loop_key] == 2


def test_step_evaluation_approve() -> None:
    evaluation = StepEvaluation(
        decision="approved",
        reasoning="Step objective met",
    )
    assert evaluation.decision == "approved"


def test_step_evaluation_revise() -> None:
    evaluation = StepEvaluation(
        decision="revise",
        reasoning="Tests are missing",
        feedback="Add unit tests",
    )
    assert evaluation.decision == "revise"
    assert evaluation.feedback == "Add unit tests"


def test_completion_config_defaults() -> None:
    config = CompletionConfig()
    assert config.evaluate is True
    assert config.max_attempts == 3
    assert config.on_exhausted == "gate"


def test_on_reject_config() -> None:
    config = OnRejectConfig(target="plan", max_loop_iterations=2)
    assert config.target == "plan"
    assert config.max_loop_iterations == 2
    assert config.on_exhausted == "gate"


def test_gate_config() -> None:
    gate = GateConfig(
        message="Review the plan",
        input=["plan"],
        options=[
            GateOption(label="Approve", action="continue"),
            GateOption(label="Request Changes", action="revise(plan)", prompt=True),
            GateOption(label="Cancel", action="cancel"),
        ],
    )
    assert len(gate.options) == 3
    assert gate.options[1].prompt is True
    assert gate.timeout_seconds == 3600
    assert gate.timeout_action == "fail"


def test_interaction_mode_none_disables_gates() -> None:
    mode = InteractionMode(mode="none")
    assert mode.mode == "none"


def test_workflow_defaults_inherited() -> None:
    workflow = Workflow(
        workflow_id="test",
        name="Test",
        defaults=WorkflowDefaults(max_attempts=5, evaluate=False, on_exhausted="fail"),
        steps=[StepDefinition(name="step1", type="run")],
    )
    assert workflow.defaults.max_attempts == 5
    assert workflow.defaults.evaluate is False
    assert workflow.defaults.on_exhausted == "fail"


def test_direct_workflow_step_complete_optional() -> None:
    """Direct workflow: step_complete is not required."""
    workflow = Workflow(
        workflow_id="system:direct",
        name="Direct",
        steps=[
            StepDefinition(
                name="execute",
                type="run",
                prompt="{user_message}",
                completion=CompletionConfig(evaluate=False),
            ),
        ],
    )
    # No evaluation = no step_complete required
    assert workflow.steps[0].completion is not None
    assert workflow.steps[0].completion.evaluate is False
