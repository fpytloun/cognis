"""Tests for the workflow engine state machine."""

from __future__ import annotations

import pytest

from cognis.core.workflow_engine import WorkflowEngine
from cognis.models.deliverable import Deliverable, DeliverableFormat, DeliverableStatus
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


@pytest.mark.asyncio
async def test_build_result_data_for_deliverable_omits_full_channel_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object.__new__(WorkflowEngine)
    large_tail = "hidden full deliverable tail"
    deliverable = Deliverable(
        deliverable_id="dlv_result",
        step_run_id="step_run",
        session_id="session",
        conversation_id="conversation",
        task_id="task",
        owner_email="user@test.com",
        content="Summary " + ("x" * 2500) + large_tail,
        format=DeliverableFormat.MARKDOWN,
        status=DeliverableStatus.APPROVED,
        title="Result",
        version=1,
    )

    async def _resolve_final_deliverable(*_: object) -> Deliverable:
        return deliverable

    monkeypatch.setattr(engine, "_resolve_final_deliverable", _resolve_final_deliverable)

    result = await engine._build_result_data(  # noqa: SLF001
        TaskModel(
            task_id="task",
            title="Task",
            created_by="user@test.com",
            agent_id="agent-1",
            status=TaskStatus.COMPLETED,
        ),
        WorkflowState(),
        Workflow(
            workflow_id="wf",
            name="Workflow",
            steps=[StepDefinition(name="deliver", type="run", prompt="")],
        ),
    )

    assert result is not None
    assert result["final_deliverable_id"] == "dlv_result"
    assert result["final_format"] == DeliverableFormat.MARKDOWN
    assert "final_channel_content" not in result
    assert len(str(result["final_content"])) <= 2000
    assert large_tail not in str(result["final_content"])


def test_evaluation_retry_reopens_terminal_todos() -> None:
    state = WorkflowState(
        last_retry_reason="evaluation_rejected",
        last_evaluation_feedback="Run the required Slack triage search.",
    )
    terminal_todos = [
        {"content": "Search Slack", "status": "completed"},
        {"content": "Write deliverable", "status": "cancelled"},
    ]

    todos = WorkflowEngine._todos_for_evaluation_retry(state, terminal_todos)

    assert todos == [
        {
            "content": (
                "Revise the step output based on evaluator feedback. Feedback: "
                "Run the required Slack triage search."
            ),
            "status": "pending",
        }
    ]


def test_evaluation_retry_preserves_non_terminal_todos() -> None:
    state = WorkflowState(
        last_retry_reason="evaluation_rejected",
        last_evaluation_feedback="Finish validation.",
    )
    active_todos = [
        {"content": "Finish validation", "status": "in_progress"},
        {"content": "Write deliverable", "status": "completed"},
    ]

    todos = WorkflowEngine._todos_for_evaluation_retry(state, active_todos)

    assert todos == active_todos


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
