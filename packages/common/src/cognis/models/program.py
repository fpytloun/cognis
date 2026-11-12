"""Versioned contracts for executor-supervised Python program execution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

PROGRAM_PROTOCOL_VERSION: Literal["cognis.program.v1"] = "cognis.program.v1"
PROGRAM_APPROVAL_RESULT_GRACE_SECONDS = 5


class ProgramModel(BaseModel):
    """Base class for strict, forward-compatible program protocol payloads."""

    model_config = ConfigDict(extra="forbid")


class ProgramLimits(ProgramModel):
    """Caller-adjustable limits; the controller clamps each value downward."""

    wall_seconds: StrictInt | None = Field(default=None, ge=1)
    max_calls: StrictInt | None = Field(default=None, ge=1)
    concurrency: StrictInt | None = Field(default=None, ge=1)
    output_bytes: StrictInt | None = Field(default=None, ge=1)


class ProgramToolDescriptor(ProgramModel):
    """An effective-plan descriptor available to a single program invocation."""

    tool_id: StrictStr = Field(min_length=1, max_length=256)
    name: StrictStr = Field(min_length=1, max_length=256)
    alias: StrictStr = Field(min_length=1, max_length=128)
    schema_hash: StrictStr = Field(min_length=1, max_length=256)
    description: StrictStr = Field(max_length=8192)
    input_schema: dict[str, Any]
    read_only: bool = True
    output_schema: dict[str, Any] | None = None


class ProgramToolPlan(ProgramModel):
    """Exact effective tool inventory for a program invocation."""

    plan_hash: StrictStr = Field(min_length=1, max_length=256)
    tools: list[ProgramToolDescriptor] = Field(default_factory=list, max_length=256)
    read_only_required: bool = True
    exclude_model_invoking: bool = False
    allowed_tool_ids: list[StrictStr] | None = None


class ProgramRuntimeBinding(ProgramModel):
    """Controller-owned identity, generation, digest, and effective plan."""

    protocol_version: Literal["cognis.program.v1"] = PROGRAM_PROTOCOL_VERSION
    program_id: StrictStr = Field(min_length=1, max_length=256)
    execution_id: StrictStr = Field(min_length=1, max_length=256)
    scope_id: StrictStr = Field(min_length=1, max_length=1024)
    expected_generation: StrictInt | None = Field(default=None, ge=1)
    description: StrictStr = Field(max_length=8192)
    mode: Literal["interactive", "workflow"] = "interactive"
    executor_policy: dict[str, Any] = Field(default_factory=dict)
    program_digest: StrictStr | None = Field(default=None, max_length=256)
    code_digest: StrictStr | None = Field(default=None, max_length=256)
    tool_manifest_digest: StrictStr = Field(min_length=1, max_length=256)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    tools: list[ProgramToolDescriptor] = Field(default_factory=list, max_length=256)
    executor_id: StrictStr = Field(min_length=1, max_length=256)
    executor_instance_id: StrictStr = Field(min_length=1, max_length=256)


class ProgramCaller(ProgramModel):
    """Controller-assigned caller linkage; never supplied in Python arguments."""

    type: Literal["program"] = "program"
    program_id: StrictStr = Field(min_length=1, max_length=256)
    parent_call_id: StrictStr = Field(min_length=1, max_length=256)


class ProgramEventMetadata(ProgramModel):
    """Top-level runtime-event linkage for a governed child tool call/result."""

    parent_call_id: StrictStr = Field(min_length=1, max_length=256)
    program_id: StrictStr = Field(min_length=1, max_length=256)
    kernel_generation: StrictInt = Field(ge=1)


class ProgramExecutionRequest(ProgramModel):
    """Outer request sent by the controller to an executor."""

    protocol_version: Literal["cognis.program.v1"] = PROGRAM_PROTOCOL_VERSION
    program_id: StrictStr = Field(min_length=1, max_length=256)
    execution_id: StrictStr = Field(min_length=1, max_length=256)
    scope_id: StrictStr = Field(min_length=1, max_length=1024)
    expected_generation: StrictInt | None = Field(default=None, ge=1)
    description: StrictStr = Field(min_length=1, max_length=8192)
    code: StrictStr
    inputs: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    executor_policy: dict[str, Any] = Field(default_factory=dict)
    program_digest: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    code_digest: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    tool_manifest_digest: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    tools: list[ProgramToolDescriptor] = Field(default_factory=list, max_length=256)
    limits: ProgramLimits = Field(default_factory=ProgramLimits)
    mode: Literal["interactive", "workflow"] = "interactive"

    @field_validator("description")
    @classmethod
    def _non_empty_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be empty")
        return value.strip()


class ProgramToolCall(ProgramModel):
    """Reverse RPC request from an active executor kernel."""

    protocol_version: Literal["cognis.program.v1"] = PROGRAM_PROTOCOL_VERSION
    program_id: StrictStr = Field(min_length=1, max_length=256)
    execution_id: StrictStr = Field(min_length=1, max_length=256)
    kernel_generation: StrictInt = Field(ge=1)
    sequence: StrictInt = Field(ge=1)
    tool_id: StrictStr = Field(min_length=1, max_length=256)
    schema_hash: StrictStr = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProgramToolValue(ProgramModel):
    """Bounded ordinary tool result returned to an awaited Python call."""

    data: Any = None
    text: StrictStr = ""
    format: Literal["structured", "text"] = "text"
    call_id: StrictStr | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    truncated: bool = False
    recovery_reference: StrictStr | None = None
    is_error: bool = False
    outcome_certainty: Literal["known", "unknown"] = "known"


class ProgramError(ProgramModel):
    """Sanitized execution failure metadata."""

    code: StrictStr = Field(min_length=1, max_length=128)
    bounded_message: StrictStr = Field(max_length=1024)
    retryable: bool = False
    outcome_certainty: Literal["known", "unknown"] = "known"


class ProgramResult(ProgramModel):
    """Outer program status/result returned to normal tool result handling."""

    protocol_version: Literal["cognis.program.v1"] = PROGRAM_PROTOCOL_VERSION
    program_id: StrictStr = Field(min_length=1, max_length=256)
    program_digest: StrictStr | None = Field(default=None, max_length=256)
    code_digest: StrictStr | None = Field(default=None, max_length=256)
    tool_manifest_digest: StrictStr | None = Field(default=None, max_length=256)
    kernel_generation: StrictInt = Field(ge=1)
    kernel_reused: bool = False
    state_lost_reason: StrictStr | None = Field(default=None, max_length=256)
    status: Literal[
        "running",
        "awaiting_approval",
        "completed",
        "failed",
        "denied",
        "cancelled",
        "timed_out",
        "budget_exceeded",
        "interrupted",
        "uncertain",
    ]
    result: Any = None
    error: ProgramError | None = None
    stdout_preview: StrictStr = Field(default="", max_length=32768)
    output_recovery_reference: StrictStr | None = None
    nested_admitted: StrictInt = Field(default=0, ge=0)
    nested_completed: StrictInt = Field(default=0, ge=0)
    nested_failed: StrictInt = Field(default=0, ge=0)
    wall_ms: StrictInt = Field(default=0, ge=0)
    output_bytes: StrictInt = Field(default=0, ge=0)
    cost_complete: bool = False
    child_call_references: list[StrictStr] = Field(default_factory=list, max_length=128)
    child_call_overflow: bool = False
