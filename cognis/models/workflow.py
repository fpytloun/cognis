"""Workflow domain models — portable process templates and runtime state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class InteractionMode(BaseModel):
    """Controls whether steps can dynamically request caller input."""

    mode: str = "explicit_gates"  # "none" | "explicit_gates" | "step_requests"


class WorkflowDefaults(BaseModel):
    """Default values inherited by all steps unless overridden."""

    max_attempts: int = 3
    evaluate: bool = True
    on_exhausted: str = "gate"  # "continue" | "fail" | "gate"


class GateOption(BaseModel):
    """A single option in a gate step."""

    label: str
    action: str  # "continue" | "revise(step_name)" | "cancel"
    prompt: bool = False


class GateConfig(BaseModel):
    """Configuration for a gate step."""

    message: str
    input: list[str] = []
    options: list[GateOption] = []


class CompletionConfig(BaseModel):
    """How a run step is verified as complete."""

    evaluate: bool = True
    evaluator_prompt: str | None = None
    max_attempts: int = 3
    on_exhausted: str = "gate"  # "continue" | "fail" | "gate"


class OnRejectConfig(BaseModel):
    """Review loop configuration — route back to a previous step on rejection."""

    target: str  # step name to re-run
    max_loop_iterations: int = 3
    on_exhausted: str = "gate"  # "continue" | "fail" | "gate"


class StepDefinition(BaseModel):
    """A single step within a workflow."""

    name: str
    type: str  # "run" | "gate"
    description: str = ""
    prompt: str = ""
    input: list[str] = []
    completion: CompletionConfig | None = None
    allow_questions: bool = False
    gate: GateConfig | None = None
    on_reject: OnRejectConfig | None = None


class Workflow(BaseModel):
    """Portable, agent-agnostic process template."""

    workflow_id: str
    name: str
    description: str = ""
    version: int = 1
    criteria: str = ""
    tags: list[str] = []
    interaction: InteractionMode = InteractionMode()
    defaults: WorkflowDefaults = WorkflowDefaults()
    steps: list[StepDefinition]
    is_system: bool = False
    owner_email: str | None = None


class StepOutput(BaseModel):
    """What a step produces when the agent calls step_complete."""

    summary: str
    outputs: dict[str, Any] = {}
    claims: list[str] = []


class StepEvaluation(BaseModel):
    """Result of the controller's semantic evaluation."""

    decision: str  # "approved" | "revise" | "failed"
    reasoning: str
    feedback: str | None = None
    evaluated_at: datetime | None = None


class WorkflowState(BaseModel):
    """Runtime workflow state persisted on the Task entity."""

    current_step_index: int = 0
    step_outputs: dict[str, dict[str, Any]] = {}
    loop_iterations: dict[str, int] = {}  # "step_a->step_b" -> count
    status: str = "running"  # "running" | "paused" | "completed" | "failed"
    last_evaluation_feedback: str | None = None  # Feedback from evaluator for retries
