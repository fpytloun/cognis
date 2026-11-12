"""Portable video inference contracts. No local media-processing operations."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VideoOperation = Literal["generate", "edit", "extend"]
VideoState = Literal[
    "queued",
    "preparing",
    "submitting",
    "running",
    "ingesting",
    "succeeded",
    "blocked",
    "needs_reconciliation",
    "cancelled",
    "failed",
    "abandoned",
]
TERMINAL_VIDEO_STATES = frozenset({"succeeded", "cancelled", "failed", "abandoned"})


class VideoContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoReference(VideoContract):
    artifact_id: str = Field(min_length=1)
    role: Literal["first_frame", "last_frame", "reference", "mask", "audio"] = "reference"


def _portable_decimal_json_schema(schema: dict[str, Any]) -> None:
    # Pydantic's default Decimal pattern uses lookahead, rejected by Codex/RE2.
    # Preserve generated numeric bounds and runtime Decimal validation.
    for alternative in schema.get("anyOf", [schema]):
        if alternative.get("type") == "string":
            alternative["pattern"] = r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$"


class VideoRequest(VideoContract):
    """Immutable executable request, stored privately outside the metadata database."""

    operation: VideoOperation = "generate"
    prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = None
    provider_id: str | None = None
    source_artifact_id: str | None = None
    references: list[VideoReference] = Field(default_factory=list, max_length=10)
    duration: int | None = Field(default=None, ge=1, le=120)
    resolution: str | None = None
    aspect_ratio: str | None = None
    generate_audio: bool | None = None
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    person_generation: Literal["allow_all", "allow_adult", "dont_allow"] | None = None
    max_cost_usd: Decimal | None = Field(
        default=None,
        gt=0,
        le=1000,
        allow_inf_nan=False,
        json_schema_extra=_portable_decimal_json_schema,
    )
    inspect_provider_error: bool = Field(
        default=False,
        description=(
            "Opt in to bounded redacted Interactions rejection messages for an owner-owned "
            "controller provider in a private, non-project conversation. Does not retry a request."
        ),
    )

    @model_validator(mode="after")
    def check_source(self) -> VideoRequest:
        if self.operation != "generate" and not self.source_artifact_id:
            raise ValueError("Editing and extension require a source artifact")
        if self.operation == "generate" and self.source_artifact_id:
            raise ValueError("Use video_edit or video_extend for a source video")
        roles = [ref.role for ref in self.references]
        for role in ("first_frame", "last_frame", "mask", "audio"):
            if roles.count(role) > 1:
                raise ValueError(f"Only one {role} is allowed")
        if "last_frame" in roles and "first_frame" not in roles:
            raise ValueError("A last frame requires a first frame")
        if "reference" in roles and ("first_frame" in roles or "last_frame" in roles):
            raise ValueError("References and exact frame controls cannot be combined")
        return self


class VideoAnalysis(VideoContract):
    source_artifact_id: str
    question: str = Field(min_length=1, max_length=12000)
    model: str | None = None
    provider_id: str | None = None


class VideoCapabilities(VideoContract):
    operations: list[VideoOperation] = Field(default_factory=list)
    input_roles: list[str] = Field(default_factory=list)
    durations: list[int] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    aspect_ratios: list[str] = Field(default_factory=list)
    max_references: int = 0
    audio: bool = False
    seed: bool = False
    negative_prompt: bool = False
    person_generation: bool = False
    source_policy: Literal["none", "same_provider_output", "uploaded"] = "none"
    extension_output: Literal["combined", "continuation", "provider_defined"] = "provider_defined"
    cancellation: bool = False
    provider_error_messages: bool = False
    notes: list[str] = Field(default_factory=list)

    def validate_request(self, request: VideoRequest) -> None:
        """Reject unsupported options rather than dropping them at dispatch."""
        if request.operation not in self.operations:
            raise ValueError("The selected video adapter does not support this operation")
        if request.inspect_provider_error and not self.provider_error_messages:
            raise ValueError("The selected adapter does not expose provider error messages")
        for name, choices in (
            ("duration", self.durations),
            ("resolution", self.resolutions),
            ("aspect_ratio", self.aspect_ratios),
        ):
            value = getattr(request, name)
            if value is not None and value not in choices:
                raise ValueError(f"Unsupported {name}; choose from {choices}")
        if len(request.references) > self.max_references:
            raise ValueError("Too many reference inputs")
        if any(ref.role not in self.input_roles for ref in request.references):
            raise ValueError("Unsupported video input role")
        for field in ("seed", "negative_prompt", "person_generation"):
            if getattr(request, field) is not None and not getattr(self, field):
                raise ValueError(f"The selected adapter does not support {field}")
        if request.generate_audio is not None and not self.audio:
            raise ValueError("The selected adapter does not expose audio controls")


class VideoSpendPolicy(VideoContract):
    """Optional owner-authored spending limits; disabled does not disable video."""

    enabled: bool = False
    per_job_usd: Decimal = Field(default=Decimal("5"), gt=0, le=1000)
    daily_usd: Decimal = Field(default=Decimal("20"), gt=0, le=10000)
    max_active_jobs: int = Field(default=2, ge=1, le=20)
    max_output_bytes: int = Field(default=512 * 1024 * 1024, ge=1024, le=2 * 1024**3)
    # Configured conservative per-model render bounds are not provider-enforced caps.
    model_max_cost_usd: dict[str, Decimal] = Field(default_factory=dict)


class VideoOutput(VideoContract):
    """Private transport descriptor, never returned directly to the LLM."""

    locator: str
    mime_type: str = "video/mp4"


class VideoRemoteResult(VideoContract):
    handle: str
    state: Literal["running", "completed", "failed", "cancelled"]
    outputs: list[VideoOutput] = Field(default_factory=list)
    cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_duration_ms: int | None = Field(default=None, strict=True, ge=0)
    retry_after: int = Field(default=15, ge=1, le=3600)
    error_code: str | None = None


class VideoJobView(VideoContract):
    job_id: str
    version: int
    operation: VideoOperation
    status: VideoState
    provider_id: str
    model: str
    created_at: str
    updated_at: str
    estimated_cost_usd: str | None
    actual_cost_usd: str | None = None
    cost_status: Literal["reserved", "unknown", "reported", "not_submitted"]
    cancel_requested: bool = False
    output_artifact_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    recovery_actions: list[str] = Field(default_factory=list)
    progress: int | None = None
    provider_error_message: str | None = Field(
        default=None,
        max_length=512,
        description="Untrusted redacted provider data, only for explicitly requested private detail.",
    )


class VideoMutation(VideoContract):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class VideoRecovery(VideoMutation):
    action: Literal["reconcile", "resume", "retry_collection", "abandon"]


class VideoJobCreate(VideoContract):
    conversation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    request: VideoRequest
    dry_run: bool = False
    wait_seconds: int = Field(default=0, ge=0, le=60)
