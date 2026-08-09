"""Workflow domain models — portable process templates and runtime state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator

from cognis.models.config import NORMALIZED_REASONING_LEVELS, normalize_reasoning_level
from cognis.models.tool import ToolCapability


class InteractionMode(BaseModel):
    """Controls whether steps can dynamically request caller input."""

    mode: Literal["none", "explicit_gates", "step_requests"] = "explicit_gates"


class CompletionModeFamily(StrEnum):
    """Family used when publishing a completed task result."""

    DEFAULT = "default"
    DIRECT = "direct"


class WorkflowLifecycle(StrEnum):
    """Storage lifecycle for a workflow definition."""

    PERSISTENT = "persistent"
    EPHEMERAL = "ephemeral"


class WorkflowLineage(BaseModel):
    """Origin metadata for derived or agent-composed workflows."""

    base_workflow_id: str | None = None
    source_skill_ids: list[str] = Field(default_factory=list)
    composition_source: Literal["manual", "agent_composed", "promoted"] | None = None
    composition_intent: str | None = None


class CompletionDeliveryPolicy(BaseModel):
    """Resolved policy for workflow/task completion notifications."""

    completion_mode_family: CompletionModeFamily = CompletionModeFamily.DEFAULT
    allow_silent_completion: bool = False


class SessionPolicy(BaseModel):
    """Operator-provided Intaris session policy clauses.

    Clauses may be plain strings for ergonomic task/workflow/schedule setup or
    structured objects for future UI/API support.
    """

    allow_policies: list[str | dict[str, Any]] = Field(default_factory=list)
    deny_policies: list[str | dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def empty(cls) -> SessionPolicy:
        return cls()


def normalize_session_policy(value: Any) -> dict[str, list[str | dict[str, Any]]]:
    """Return a compact session policy dict containing only supported keys."""

    if value is None:
        return {}
    policy = value if isinstance(value, SessionPolicy) else SessionPolicy.model_validate(value)
    result: dict[str, list[str | dict[str, Any]]] = {}
    if policy.allow_policies:
        result["allow_policies"] = policy.allow_policies
    if policy.deny_policies:
        result["deny_policies"] = policy.deny_policies
    return result


def merge_session_policies(*policies: Any) -> dict[str, list[str | dict[str, Any]]]:
    """Merge session policies in precedence order without interpreting text."""

    merged: dict[str, list[str | dict[str, Any]]] = {
        "allow_policies": [],
        "deny_policies": [],
    }
    for policy in policies:
        normalized = normalize_session_policy(policy)
        merged["allow_policies"].extend(normalized.get("allow_policies", []))
        merged["deny_policies"].extend(normalized.get("deny_policies", []))
    return {key: value for key, value in merged.items() if value}


def resolve_completion_delivery_policy(
    workflow_defaults: WorkflowDefaults | None,
    *,
    task_policy: CompletionDeliveryPolicy | None = None,
) -> CompletionDeliveryPolicy:
    """Resolve the effective completion delivery policy for a task or step."""

    if task_policy is not None:
        return task_policy
    if workflow_defaults is not None:
        return workflow_defaults.delivery
    return CompletionDeliveryPolicy()


class WorkflowDefaults(BaseModel):
    """Default values inherited by all steps unless overridden."""

    max_attempts: int = 3
    evaluate: bool = True
    on_exhausted: Literal["continue", "fail", "gate"] = "gate"
    delivery: CompletionDeliveryPolicy = Field(default_factory=CompletionDeliveryPolicy)
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)


class GateOption(BaseModel):
    """A single option in a gate step."""

    label: str
    action: str  # "continue" | "revise(step_name)" | "cancel" — free-form for revise()
    prompt: bool = False


class GateCondition(BaseModel):
    """Declarative condition controlling whether a gate should fire."""

    expression: str


class GateConfig(BaseModel):
    """Configuration for a gate step.

    Note: ``GateConfig.input`` remains ``list[str]`` — it references
    step names whose outputs populate gate context. This is intentionally
    separate from ``StepDefinition.input`` (``StepInputConfig``).
    """

    message: str
    input: list[str] = []
    options: list[GateOption] = []
    conditions: list[GateCondition] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 3600
    timeout_action: Literal["fail", "continue", "cancel"] = "fail"


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


class OutcomeRoute(BaseModel):
    """Route to apply when a completed step reports a non-success outcome."""

    status: Literal["success", "rejected", "failed"]
    action: str
    max_loop_iterations: int | None = None
    on_exhausted: Literal["continue", "fail", "gate"] = "gate"


class StepOutcome(BaseModel):
    """Business outcome reported by ``step_complete`` after proper step execution."""

    status: Literal["success", "rejected", "failed"] = "success"
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_reason_requirement(self) -> StepOutcome:
        if self.status in {"rejected", "failed"} and not (self.reason or "").strip():
            raise ValueError("outcome.reason is required when outcome.status is rejected or failed")
        return self


class StepCompletionNotification(BaseModel):
    """Optional completion delivery choice requested by the step."""

    mode: Literal["silent", "direct"]
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_reason_requirement(self) -> StepCompletionNotification:
        if self.mode == "silent" and not (self.reason or "").strip():
            raise ValueError("notification.reason is required when notification.mode is silent")
        return self


# ---------------------------------------------------------------------------
# Step input context model
# ---------------------------------------------------------------------------


class StepInputConfig(BaseModel):
    """Controls what context flows from previous steps into a step.

    ``type`` determines *how* context is assembled:

    * ``"null"``    — nothing from previous steps.
    * ``"full"``    — complete event history from **one** source step session.
    * ``"summary"`` — durable step summary plus deliverable/structured outputs.
    * ``"last"``    — richer ``step_complete`` output(s), including claims.

    ``source`` names the step(s) whose output is referenced.  For ``"full"``
    only a single source is allowed.  ``None`` means the engine will default
    to the previous step (for ``"last"``) or no source (for ``"null"``).

    ``reuse_session_from`` names one included source step whose conversation
    and session continue into the target step.
    """

    type: Literal["null", "full", "summary", "last"] = "last"
    source: str | list[str] | None = None
    reuse_session_from: str | None = None

    @field_validator("source")
    @classmethod
    def _validate_full_single_source(
        cls, value: str | list[str] | None, info: Any
    ) -> str | list[str] | None:
        """Reject list sources for ``type="full"``."""
        input_type = info.data.get("type") if info.data else None
        all_source = value == "all" or (
            isinstance(value, list) and any(str(item).strip() == "all" for item in value)
        )
        if input_type == "full" and isinstance(value, list):
            raise ValueError("StepInputConfig type='full' only accepts a single source, not a list")
        if input_type == "full" and all_source:
            raise ValueError("StepInputConfig type='full' cannot use source='all'")
        if isinstance(value, list):
            normalized = [str(item).strip() for item in value if str(item).strip()]
            if "all" in normalized and len(normalized) > 1:
                raise ValueError("StepInputConfig source='all' must be used alone")
        return value

    @field_validator("reuse_session_from")
    @classmethod
    def _validate_reuse_session_from(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("StepInputConfig reuse_session_from must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_reuse_input_type(self) -> StepInputConfig:
        if self.reuse_session_from is not None and self.type == "null":
            raise ValueError("StepInputConfig type='null' cannot reuse a prior session")
        return self

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


class StepProfileMode(StrEnum):
    """How a step profile constrains the tool inventory."""

    SOFT = "soft"
    HARD = "hard"


class StepToolOverrides(BaseModel):
    """Explicit per-tool includes/excludes on top of profile rules."""

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class StepProfileConfig(BaseModel):
    """Inline profile matrix and tool overrides for a step."""

    matrix: dict[str, list[ToolCapability]] = Field(default_factory=dict)
    tool_overrides: StepToolOverrides = Field(default_factory=StepToolOverrides)
    allow_tool_search: bool = True


class StepRevisionConfig(BaseModel):
    """Controls whether a step can be targeted by human revisions."""

    allowed: bool = True
    use_when: str = ""


class StepCompletionMetadataField(BaseModel):
    """One field in a step_complete metadata contract."""

    name: str
    type: Literal["string", "number", "boolean", "array", "object"]
    required: bool = False
    description: str = ""
    enum: list[str] | None = None


class StepCompletionContract(BaseModel):
    """Structured metadata the step must or may emit on completion."""

    fields: list[StepCompletionMetadataField] = Field(default_factory=list)


class DeterministicOutputConfig(BaseModel):
    """Rendered output emitted by a deterministic step or skip."""

    model_config = {"extra": "forbid"}

    summary: str
    content: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallStepConfig(BaseModel):
    """Definition for one controller-owned tool invocation."""

    model_config = {"extra": "forbid"}

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    fail_on_error: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    allow_side_effects: bool = False
    redact_args: list[str] = Field(default_factory=list)

    @field_validator("tool")
    @classmethod
    def _validate_tool(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool_call.tool must not be empty")
        return value


class ConditionStepConfig(BaseModel):
    """Boolean expression and optional named branches."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    if_: str = Field(alias="if")
    then: str | None = None
    else_: str | None = Field(default=None, alias="else")
    output: DeterministicOutputConfig | None = None
    revision_source: str | None = None
    max_loop_iterations: int | None = Field(default=None, ge=1, le=100)
    on_exhausted: Literal["continue", "fail", "gate"] = "gate"

    @field_validator("if_")
    @classmethod
    def _validate_expression(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("condition.if must not be empty")
        return value

    @model_serializer
    def _serialize(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"if": self.if_}
        if self.then is not None:
            payload["then"] = self.then
        if self.else_ is not None:
            payload["else"] = self.else_
        if self.output is not None:
            payload["output"] = self.output.model_dump(mode="json", exclude_none=True)
        if self.revision_source is not None:
            payload["revision_source"] = self.revision_source
        if self.max_loop_iterations is not None:
            payload["max_loop_iterations"] = self.max_loop_iterations
            payload["on_exhausted"] = self.on_exhausted
        return payload


class CompleteStepConfig(BaseModel):
    """Terminal deterministic workflow result."""

    model_config = {"extra": "forbid"}

    status: Literal["completed", "failed"] = "completed"
    summary: str
    content: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    notification: StepCompletionNotification | None = None
    delivery_mode_override: (
        Literal[
            "same_conversation",
            "preferred_channel",
            "latest_active_for_agent",
            "specific_conversation",
            "silent",
        ]
        | None
    ) = None

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("complete.summary must not be empty")
        return value


class StepDefinition(BaseModel):
    """A single step within a workflow."""

    name: str
    type: Literal["run", "gate", "tool_call", "condition", "complete"]
    description: str = ""
    prompt: str = ""
    objective: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    defer_to: list[str] = Field(default_factory=list)
    agent_override: str | None = None  # Secondary agent ID for this step
    agent_profile_id: str | None = None
    reasoning_effort: str | None = None
    input: StepInputConfig | None = None
    completion: CompletionConfig | None = None
    allow_questions: bool = False
    step_profile_id: str | None = None
    step_profile_mode: StepProfileMode = StepProfileMode.SOFT
    step_profile: StepProfileConfig | None = None
    gate: GateConfig | None = None
    on_reject: OnRejectConfig | None = None
    outcome_routes: list[OutcomeRoute] = Field(default_factory=list)
    require_deliverable: bool = True
    revision: StepRevisionConfig = Field(default_factory=StepRevisionConfig)
    metadata_contract: StepCompletionContract | None = None
    when: str | None = None
    on_skip: DeterministicOutputConfig | None = None
    on_error: Literal["fail", "continue", "skip", "gate"] | None = None
    next: str | None = None
    tool_call: ToolCallStepConfig | None = None
    condition: ConditionStepConfig | None = None
    complete: CompleteStepConfig | None = None

    @model_validator(mode="after")
    def _validate_step_type_contract(self) -> StepDefinition:
        configs = {
            "gate": self.gate,
            "tool_call": self.tool_call,
            "condition": self.condition,
            "complete": self.complete,
        }
        required = configs.get(self.type)
        if self.type in {"tool_call", "condition", "complete"} and required is None:
            raise ValueError(f"{self.type} step {self.name!r} requires {self.type} configuration")
        mixed = [
            name for name, config in configs.items() if config is not None and name != self.type
        ]
        if mixed:
            raise ValueError(
                f"{self.type} step {self.name!r} has incompatible configuration: {mixed}"
            )

        deterministic = self.type in {"tool_call", "condition", "complete"}
        if not deterministic and any(
            value is not None for value in (self.when, self.on_skip, self.on_error, self.next)
        ):
            raise ValueError(
                f"{self.type} step {self.name!r} cannot use deterministic control fields"
            )
        if deterministic and any(
            value is not None
            for value in (
                self.agent_override,
                self.agent_profile_id,
                self.reasoning_effort,
                self.input,
                self.completion,
                self.on_reject,
                self.metadata_contract,
            )
        ):
            raise ValueError(
                f"deterministic step {self.name!r} cannot use agent/input/completion/review fields"
            )
        if deterministic and (self.outcome_routes or self.allow_questions):
            raise ValueError(
                f"deterministic step {self.name!r} cannot use outcome routing or questions"
            )
        incompatible_values = []
        if self.prompt:
            incompatible_values.append("prompt")
        if self.objective is not None:
            incompatible_values.append("objective")
        if self.responsibilities:
            incompatible_values.append("responsibilities")
        if self.defer_to:
            incompatible_values.append("defer_to")
        if self.step_profile_id is not None:
            incompatible_values.append("step_profile_id")
        if self.step_profile_mode != StepProfileMode.SOFT:
            incompatible_values.append("step_profile_mode")
        if self.step_profile is not None:
            incompatible_values.append("step_profile")
        if self.revision != StepRevisionConfig():
            incompatible_values.append("revision")
        if not self.require_deliverable:
            incompatible_values.append("require_deliverable")
        if deterministic and incompatible_values:
            raise ValueError(
                f"deterministic step {self.name!r} has incompatible fields: "
                f"{sorted(incompatible_values)}"
            )
        if self.type == "complete" and self.next is not None:
            raise ValueError(f"complete step {self.name!r} cannot define next")
        if self.type == "condition" and self.next is not None:
            raise ValueError(
                f"condition step {self.name!r} uses condition.then/else instead of next"
            )
        if self.when is not None and not self.when.strip():
            raise ValueError("when must not be empty")
        return self

    @field_validator("objective")
    @classmethod
    def _validate_objective(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("objective must not be empty")
        return normalized

    @field_validator("responsibilities", "defer_to")
    @classmethod
    def _validate_non_empty_unique_items(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("items must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("items must not contain duplicates")
        return normalized

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, value: str | None) -> str | None:
        """Reject reasoning_effort values outside the normalised set.

        Silent typos (e.g. ``"medimum"``) would otherwise be dropped at the
        provider layer with no signal to the author. Raise early at load
        time so workflow edits fail loudly.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("reasoning_effort must be a string or null")
        normalized = normalize_reasoning_level(value)
        if normalized is None:
            if not value.strip():
                return None
            allowed = ", ".join(NORMALIZED_REASONING_LEVELS)
            raise ValueError(f"reasoning_effort must be one of {allowed}; got {value!r}")
        return normalized

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


class WorkflowPhaseDefinition(BaseModel):
    """Presentation-only grouping over a contiguous range of workflow steps."""

    id: str
    title: str
    description: str = ""
    step_names: list[str]

    @field_validator("id", "title")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("step_names")
    @classmethod
    def _validate_non_empty_steps(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("phase must contain at least one step")
        if len(value) != len(set(value)):
            raise ValueError("phase step_names must not contain duplicates")
        return value


class WorkflowPresentation(BaseModel):
    """Optional author-defined phase presentation for a workflow."""

    phases: list[WorkflowPhaseDefinition]

    @field_validator("phases")
    @classmethod
    def _validate_non_empty_phases(
        cls, value: list[WorkflowPhaseDefinition]
    ) -> list[WorkflowPhaseDefinition]:
        if not value:
            raise ValueError("presentation must contain at least one phase")
        phase_ids = [phase.id for phase in value]
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("phase ids must be unique")
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
    presentation: WorkflowPresentation | None = None
    is_system: bool = False
    owner_email: str | None = None
    lifecycle: WorkflowLifecycle = WorkflowLifecycle.PERSISTENT
    archived_at: datetime | None = None
    lineage: WorkflowLineage | None = None
    allow_user_override: bool = Field(default=False, exclude=True)
    allow_user_disable: bool = Field(default=False, exclude=True)
    editable_fields: list[str] = Field(default_factory=list, exclude=True)
    has_overrides: bool = Field(default=False, exclude=True)
    disabled: bool = Field(default=False, exclude=True)
    override_warnings: list[str] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _validate_presentation(self) -> Workflow:
        if self.presentation is None:
            return self

        canonical_names = [step.name for step in self.steps]
        phase_names = [
            step_name for phase in self.presentation.phases for step_name in phase.step_names
        ]
        unknown = [name for name in phase_names if name not in canonical_names]
        if unknown:
            raise ValueError(f"presentation references unknown steps: {unknown}")
        if len(phase_names) != len(set(phase_names)):
            raise ValueError("each workflow step must belong to exactly one phase")
        missing = [name for name in canonical_names if name not in phase_names]
        if missing:
            raise ValueError(f"presentation is missing workflow steps: {missing}")
        if phase_names != canonical_names:
            raise ValueError(
                "phase step_names and phase order must preserve canonical workflow step order"
            )
        return self


def canonical_workflow_digest(workflow: Workflow | dict[str, Any]) -> str:
    """Return the SHA-256 digest of a canonical effective workflow definition."""

    definition = (
        workflow.model_dump(mode="json", exclude_none=True)
        if isinstance(workflow, Workflow)
        else workflow
    )
    canonical = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class StepOutput(BaseModel):
    """What a step produces on completion (via step_complete) or failure (error is set)."""

    summary: str
    content: str = ""  # Approved deliverable text mirror for downstream context.
    outputs: dict[str, Any] = {}
    metadata: dict[str, Any] = Field(default_factory=dict)
    claims: list[str] = []
    outcome: StepOutcome | None = None
    notification: StepCompletionNotification | None = None
    deliverable_id: str | None = None
    deliverable_version: int | None = None
    deliverable_format: Literal["markdown", "plain", "html", "rich"] | None = None
    deliverable_title: str | None = None
    execution_evidence: dict[str, Any] | None = None
    error: str | None = None  # Set when the step failed with an exception
    completed_at: datetime | None = None
    session_id: str | None = None
    intaris_session_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be empty")
        return value


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
    routing_skips: dict[str, str] = Field(default_factory=dict)
    effective_workflow_version: int | None = None
    effective_workflow_digest: str | None = None
    effective_workflow_definition: dict[str, Any] | None = None
    last_evaluation_feedback: str | None = None  # Feedback from evaluator for retries
    last_retry_reason: (
        Literal["execution_failed", "evaluation_rejected", "routed_revision"] | None
    ) = None
    last_revision_context: str | None = None  # Full reviewer output for backward revisions
    last_operator_instruction: str | None = None  # One-shot human instruction for next step
    pending_pause_type: (
        Literal["gate", "step_input", "credential_request", "auth_challenge"] | None
    ) = None
    pending_pause_payload: dict[str, Any] | None = None
    current_step_status: Literal["running", "paused"] | None = None

    @field_validator("last_retry_reason", mode="before")
    @classmethod
    def _normalize_last_retry_reason(cls, value: Any) -> str | None:
        """Ignore stale persisted retry reasons from older controller versions."""

        if value in {"execution_failed", "evaluation_rejected", "routed_revision"}:
            return str(value)
        return None

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


def pin_effective_workflow(state: WorkflowState, workflow: Workflow) -> WorkflowState:
    """Pin an effective definition once and return the mutated runtime state."""

    if state.effective_workflow_definition is not None:
        return state
    definition = workflow.model_dump(mode="json", exclude_none=True)
    state.effective_workflow_version = workflow.version
    state.effective_workflow_digest = canonical_workflow_digest(definition)
    state.effective_workflow_definition = definition
    return state


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
        return StepInputConfig(
            type=config.type,
            source=prev_name,
            reuse_session_from=config.reuse_session_from,
        )

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
    source_names = effective.source_names()
    if source_names != ["all"]:
        return source_names
    return [
        workflow_steps[index].name
        for index in range(step_index)
        if workflow_steps[index].type == "run"
    ]
