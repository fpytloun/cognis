"""Workflow domain models — portable process templates and runtime state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class InteractionMode(BaseModel):
    """Controls whether steps can dynamically request caller input."""

    mode: Literal["none", "explicit_gates", "step_requests"] = "explicit_gates"


class WorkflowDefaults(BaseModel):
    """Default values inherited by all steps unless overridden."""

    max_attempts: int = 3
    evaluate: bool = True
    on_exhausted: Literal["continue", "fail", "gate"] = "gate"


class GateOption(BaseModel):
    """A single option in a gate step."""

    label: str
    action: str  # "continue" | "revise(step_name)" | "cancel" — free-form for revise()
    prompt: bool = False


class GateConfig(BaseModel):
    """Configuration for a gate step.

    Note: ``GateConfig.input`` remains ``list[str]`` — it references
    step names whose outputs populate gate context. This is intentionally
    separate from ``StepDefinition.input`` (``StepInputConfig``).
    """

    message: str
    input: list[str] = []
    options: list[GateOption] = []


class CompletionConfig(BaseModel):
    """How a run step is verified as complete."""

    evaluate: bool = True
    evaluator_prompt: str | None = None
    max_attempts: int = 3
    on_exhausted: Literal["continue", "fail", "gate"] = "gate"


class OnRejectConfig(BaseModel):
    """Review loop configuration — route back to a previous step on rejection."""

    target: str  # step name to re-run
    max_loop_iterations: int = 3
    on_exhausted: Literal["continue", "fail", "gate"] = "gate"


# ---------------------------------------------------------------------------
# Step input context model
# ---------------------------------------------------------------------------


class StepInputConfig(BaseModel):
    """Controls what context flows from previous steps into a step.

    ``type`` determines *how* context is assembled:

    * ``"null"``    — nothing from previous steps.
    * ``"full"``    — complete event history from **one** source step session.
    * ``"summary"`` — LLM-generated summary of source step session(s).
    * ``"last"``    — ``step_complete`` output(s) from source step(s).

    ``source`` names the step(s) whose output is referenced.  For ``"full"``
    only a single source is allowed.  ``None`` means the engine will default
    to the previous step (for ``"last"``) or no source (for ``"null"``).
    """

    type: Literal["null", "full", "summary", "last"] = "last"
    source: str | list[str] | None = None

    @field_validator("source")
    @classmethod
    def _validate_full_single_source(
        cls, value: str | list[str] | None, info: Any
    ) -> str | list[str] | None:
        """Reject list sources for ``type="full"``."""
        input_type = info.data.get("type") if info.data else None
        if input_type == "full" and isinstance(value, list):
            raise ValueError("StepInputConfig type='full' only accepts a single source, not a list")
        return value

    def source_names(self) -> list[str]:
        """Return the source step name(s) as a normalised list."""
        if self.source is None:
            return []
        if isinstance(self.source, str):
            return [self.source]
        return list(self.source)

    def single_source(self) -> str | None:
        """Return the single source step name, or None."""
        if self.source is None:
            return None
        if isinstance(self.source, str):
            return self.source
        return self.source[0] if self.source else None


class StepDefinition(BaseModel):
    """A single step within a workflow."""

    name: str
    type: Literal["run", "gate"]
    description: str = ""
    prompt: str = ""
    agent_override: str | None = None  # Secondary agent ID for this step
    input: StepInputConfig | None = None
    completion: CompletionConfig | None = None
    allow_questions: bool = False
    gate: GateConfig | None = None
    on_reject: OnRejectConfig | None = None

    @field_validator("input", mode="before")
    @classmethod
    def _coerce_legacy_input(cls, value: Any) -> Any:
        """Coerce legacy ``list[str]`` or ``str`` inputs.

        Existing workflows may store ``input`` as a plain list of step names
        (e.g. ``["plan"]``).  This validator converts them to an equivalent
        ``StepInputConfig(type="last", source=...)``.
        """
        if value is None:
            return value
        if isinstance(value, StepInputConfig):
            return value
        if isinstance(value, dict):
            return value  # let Pydantic handle it
        if isinstance(value, str):
            return {"type": "last", "source": value}
        if isinstance(value, list):
            str_items = [item for item in value if isinstance(item, str)]
            if not str_items:
                return None
            if len(str_items) == 1:
                return {"type": "last", "source": str_items[0]}
            return {"type": "last", "source": str_items}
        return value


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
    """What a step produces on completion (via step_complete) or failure (error is set)."""

    summary: str
    content: str = ""  # Full assistant output (for delegation result delivery)
    outputs: dict[str, Any] = {}
    claims: list[str] = []
    error: str | None = None  # Set when the step failed with an exception
    completed_at: datetime | None = None
    session_id: str | None = None
    intaris_session_id: str | None = None


class StepEvaluation(BaseModel):
    """Result of the controller's semantic evaluation."""

    decision: Literal["approved", "revise", "failed"]
    reasoning: str
    feedback: str | None = None
    evaluated_at: datetime | None = None


class WorkflowState(BaseModel):
    """Runtime workflow state persisted on the Task entity."""

    current_step_index: int = 0
    step_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    loop_iterations: dict[str, int] = Field(default_factory=dict)  # "step_a->step_b" -> count
    version: int = 0  # Optimistic concurrency — incremented on each persist
    status: Literal["running", "paused", "completed", "failed", "cancelled"] = "running"
    skipped_steps: list[str] = Field(default_factory=list)  # Steps skipped due to exhaustion
    last_evaluation_feedback: str | None = None  # Feedback from evaluator for retries
    pending_pause_type: Literal["gate", "step_input"] | None = None
    pending_pause_payload: dict[str, Any] | None = None
    current_step_status: Literal["running", "paused"] | None = None

    def get_source_intaris_session_id(self, step_name: str) -> str:
        """Resolve the Intaris session ID from a completed source step.

        Raises ``ValueError`` if the step output is missing or lacks the
        required ``intaris_session_id`` metadata.
        """
        raw = self.step_outputs.get(step_name)
        if raw is None:
            raise ValueError(f"No output found for source step {step_name!r}")
        session_id = raw.get("intaris_session_id")
        if not session_id:
            raise ValueError(f"Source step {step_name!r} output missing intaris_session_id")
        return str(session_id)


# ---------------------------------------------------------------------------
# Shared helpers for step-input source resolution
# ---------------------------------------------------------------------------


def _find_previous_run_step(
    step_index: int,
    workflow_steps: list[StepDefinition],
) -> str | None:
    """Walk backwards from *step_index* to find the nearest ``type="run"`` step.

    Gate steps never produce output, so they are skipped when resolving
    the default source for step input.
    """
    for i in range(step_index - 1, -1, -1):
        if workflow_steps[i].type == "run":
            return workflow_steps[i].name
    return None


def resolve_effective_input(
    step_def: StepDefinition,
    step_index: int,
    workflow_steps: list[StepDefinition],
) -> StepInputConfig:
    """Return the effective ``StepInputConfig`` for a step.

    Applies default resolution:
    * If ``step_def.input`` is ``None`` and the step is the first → ``null``.
    * If ``step_def.input`` is ``None`` and the step is not the first →
      ``last`` from the previous step.
    * If ``step_def.input`` is explicitly set with ``type="last"`` (or
      ``"summary"``) but ``source`` is ``None`` → default to the previous
      step.
    * ``type="null"`` never has a source.
    * Otherwise use the explicit config.
    """
    if step_def.input is None:
        if step_index == 0:
            return StepInputConfig(type="null")
        prev_name = _find_previous_run_step(step_index, workflow_steps)
        if prev_name is None:
            return StepInputConfig(type="null")
        return StepInputConfig(type="last", source=prev_name)

    config = step_def.input

    # Normalize missing source for types that require one
    if config.type in ("last", "summary", "full") and config.source is None:
        if step_index == 0:
            # First step with an input type that needs a source but has none —
            # treat as null.
            return StepInputConfig(type="null")
        prev_name = _find_previous_run_step(step_index, workflow_steps)
        if prev_name is None:
            return StepInputConfig(type="null")
        return StepInputConfig(type=config.type, source=prev_name)

    return config


def resolve_source_names(
    step_def: StepDefinition,
    step_index: int,
    workflow_steps: list[StepDefinition],
) -> list[str]:
    """Return the resolved source step names for a step definition.

    This is the single source of truth for iterating input sources.
    Use this everywhere instead of iterating ``step_def.input`` directly.
    """
    effective = resolve_effective_input(step_def, step_index, workflow_steps)
    return effective.source_names()
