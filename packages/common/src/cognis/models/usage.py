"""Portable, content-free accounting contracts shared by controller and executor.

These models carry observations, not authorization. Only trusted adapters may
construct verified identity/resource bindings. Queue acceptance is not durability;
capture must never gate inference or cause inference replay.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Protocol, Self, get_args

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

UsageId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@/-]{1,160}$")]
UsageSubject = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@/+%-]{1,320}$")]
UsageLabel = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:/ -]{1,128}$")]
UsageDigest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
UsageInteger = Annotated[StrictInt, Field(ge=0, le=9_223_372_036_854_775_807)]
UsageMilliseconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
UsageOutcome = Literal["success", "error", "cancelled", "not_dispatched", "unknown"]
UsageCoverage = Literal["reported", "partial", "unknown", "not_applicable"]
UsageSource = Literal["provider", "native_sdk", "adapter"]
UsageCostBasis = Literal["configured_estimate", "api_equivalent", "provider_reported"]
UsageMediaOperation = Literal[
    "image_generate", "image_edit", "transcribe", "synthesize", "video_generate", "video_extend"
]
UsageOperation = Literal[
    "chat",
    "responses",
    "embeddings",
    "systemone",
    "native",
    "image_generate",
    "image_edit",
    "transcribe",
    "synthesize",
    "video_generate",
    "video_extend",
]
USAGE_OBSERVATION_MAX_BYTES = 8192
USAGE_CONTEXT_TARGET_BYTES = 4864
USAGE_TERMINAL_RESERVE_BYTES = 3328
USAGE_MODEL_NAME_MAX_CHARS = 512
USAGE_PARAMETER_PROVENANCE_MAX_ITEMS = 18


def _display_label(value: str) -> str:
    if any(
        unicodedata.category(char) in {"Cc", "Cs"}
        or (unicodedata.category(char) == "Cf" and char != "\u200d")
        for char in value
    ):
        raise ValueError("Display labels cannot contain control characters")
    return value


UsageDisplayLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=128), AfterValidator(_display_label)
]
UsageModelName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=USAGE_MODEL_NAME_MAX_CHARS),
    AfterValidator(_display_label),
]
UsageClientOperation = Literal[
    "summary_window",
    "compaction",
    "l3",
    "intention",
    "source_context",
    "policy_compile",
    "policy_assess",
    "alignment",
    "evaluation",
    "judge",
    "search_embedding",
    "extraction",
    "query_generation",
    "reranking",
    "consolidation",
    "embeddings",
    "unknown",
    "extract",
    "remember_extract",
    "remember_dedup",
    "compact",
    "shorten",
    "reclassify",
    "evidence_semantic_equivalence",
    "classify",
    "query_gen",
    "rerank",
    "answer",
    "fsck_security",
    "fsck_dedup",
    "fsck_content_quality",
    "fsck_metadata_normalization",
    "embed",
    "embed_batch",
]


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UsageTimestamp = Annotated[AwareDatetime, AfterValidator(_utc)]


class UsageModel(BaseModel):
    """Strict frozen contract; nested collections are immutable tuples."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    def observation_digest(self) -> str:
        """Digest canonical typed bytes, independent of JSON input field order."""
        return hashlib.sha256(
            self.model_dump_json(exclude_none=True, exclude_defaults=True).encode()
        ).hexdigest()


class UsageTrace(UsageModel):
    """Logical correlation remains distinct from a real transport attempt."""

    request_id: UsageId
    cycle_id: UsageId
    attempt_id: UsageId
    attempt_ordinal: UsageInteger
    retry_of_attempt_id: UsageId | None = None
    fallback_from_attempt_id: UsageId | None = None
    parent_operation_id: UsageId | None = None
    execution_id: UsageId | None = None


class UsageIdentity(UsageModel):
    """Trusted principal, attributed consumer and payer are separate identities."""

    principal_subject: UsageSubject | None = None
    principal_type: Literal["user", "service", "unattributed"]
    usage_subject_kind: Literal["user", "service", "unattributed"]
    usage_subject_id: UsageSubject | None = None
    on_behalf_user: UsageSubject | None = None
    initiating_actor: UsageSubject | None = None
    resource_owner: UsageSubject | None = None
    provider_payer: UsageSubject | None = None
    routing_owner: UsageSubject | None = None
    auth_type: UsageLabel | None = None
    service_id: UsageId | None = None
    service_operation: UsageLabel | None = None
    client_id: UsageId | None = None
    key_record_id: UsageId | None = None
    token_record_id: UsageId | None = None
    traffic_origin: Literal["cognis", "api", "unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_subject(self) -> Self:
        if (self.usage_subject_kind == "unattributed") != (self.usage_subject_id is None):
            raise ValueError("A known usage subject requires its validated identity")
        if self.principal_type != "unattributed" and self.principal_subject is None:
            raise ValueError("An authenticated principal requires its subject")
        if self.on_behalf_user is not None and (
            self.usage_subject_kind != "user" or self.usage_subject_id != self.on_behalf_user
        ):
            raise ValueError("On-behalf attribution must match the validated usage subject")
        return self

    @property
    def usage_subject_key(self) -> str:
        """Return a stable typed bucket, without fabricating a user email."""
        return f"{self.usage_subject_kind}:{self.usage_subject_id or ''}"


class UsageResourceBinding(UsageModel):
    """Verified execution binding, never populated directly from client headers."""

    provenance: Literal["controller", "authorized_binding"]
    project_id: UsageId | None = None
    conversation_id: UsageId | None = None
    session_id: UsageId | None = None
    activity_scope_id: UsageId | None = None
    task_id: UsageId | None = None
    step_run_id: UsageId | None = None
    turn_id: UsageId | None = None
    agent_id: UsageId | None = None
    agent_profile_id: UsageId | None = None


class UsageClientLabels(UsageModel):
    """Format-validated, explicitly unverified client correlation labels."""

    operation: UsageClientOperation | None = None
    operation_id: UsageId | None = None
    client_request_id: UsageId | None = None
    client_version: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")] | None
    ) = None
    project_id: UsageId | None = None
    session_id: UsageId | None = None
    task_id: UsageId | None = None

    @field_validator("operation", mode="before")
    @classmethod
    def ignore_unknown_operation(cls, value: object) -> object:
        """Unknown external labels never discard the enclosing observation."""
        from typing import get_args

        allowed = get_args(UsageClientOperation)
        return value if isinstance(value, str) and value in allowed else None


class UsageHookOrigin(UsageModel):
    """Stable trusted hook metadata, preserved after configuration changes."""

    hook_id: UsageId
    hook_revision: UsageInteger | None = None
    hook_name: UsageDisplayLabel | None = None
    hook_type: UsageLabel | None = None
    invocation_id: UsageId | None = None
    occurrence: UsageId | None = None


UsageParameterName = Literal[
    "temperature",
    "top_p",
    "top_k",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "reasoning_effort",
    "reasoning_budget",
    "reasoning_enabled",
    "service_tier",
    "image_size",
    "image_quality",
    "response_format",
    "tool_choice",
]
UsageParameterSource = Literal[
    "request",
    "model_config",
    "route",
    "profile",
    "provider_default",
    "adapter_override",
    "unknown",
]


class UsageParameterProvenance(UsageModel):
    """Origin of one safe scalar; no provider kwargs or payload dictionaries."""

    parameter: UsageParameterName
    source: UsageParameterSource


class UsageModelParameters(UsageModel):
    """Safe requested/effective scalar parameters; omitted is unknown, not default."""

    temperature: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    top_p: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    top_k: UsageInteger | None = None
    max_input_tokens: UsageInteger | None = None
    max_output_tokens: UsageInteger | None = None
    max_total_tokens: UsageInteger | None = None
    seed: Annotated[StrictInt, Field(ge=-(2**63), le=2**63 - 1)] | None = None
    frequency_penalty: Annotated[float, Field(allow_inf_nan=False)] | None = None
    presence_penalty: Annotated[float, Field(allow_inf_nan=False)] | None = None
    repetition_penalty: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    reasoning_effort: UsageLabel | None = None
    reasoning_budget: UsageInteger | None = None
    reasoning_enabled: StrictBool | None = None
    service_tier: UsageLabel | None = None
    image_size: UsageLabel | None = None
    image_quality: UsageLabel | None = None
    response_format: Literal["text", "json_object", "json_schema", "other"] | None = None
    tool_choice: Literal["auto", "none", "required", "named", "other"] | None = None
    provenance: Annotated[
        tuple[UsageParameterProvenance, ...],
        Field(max_length=USAGE_PARAMETER_PROVENANCE_MAX_ITEMS),
    ] = ()
    provenance_default: UsageParameterSource | None = None

    @model_validator(mode="before")
    @classmethod
    def compact_complete_provenance(cls, data: object) -> object:
        """Losslessly encode a complete provenance snapshot, never drop values."""
        if not isinstance(data, dict) or data.get("provenance_default") is not None:
            return data
        supplied = data.get("provenance")
        if (
            not isinstance(supplied, (list, tuple))
            or not 2 <= len(supplied) <= USAGE_PARAMETER_PROVENANCE_MAX_ITEMS
        ):
            return data
        entries = [
            item.model_dump() if isinstance(item, UsageParameterProvenance) else item
            for item in supplied
        ]
        names = get_args(UsageParameterName)
        sources = get_args(UsageParameterSource)
        if any(
            not isinstance(item, dict)
            or set(item) != {"parameter", "source"}
            or item["parameter"] not in names
            or item["source"] not in sources
            for item in entries
        ):
            return data
        present = {name for name in names if data.get(name) is not None}
        provided = [item["parameter"] for item in entries]
        if len(set(provided)) != len(provided) or set(provided) != present:
            return data
        counts = {source: sum(item["source"] == source for item in entries) for source in sources}
        default = max(sources, key=lambda source: (counts[source], source))
        if counts[default] < 2:
            return data
        return data | {
            "provenance_default": default,
            "provenance": [item for item in entries if item["source"] != default],
        }

    def provenance_source(self, parameter: UsageParameterName) -> UsageParameterSource | None:
        """Resolve compact/default provenance for one actually present scalar."""
        if getattr(self, parameter) is None:
            return None
        for item in self.provenance:
            if item.parameter == parameter:
                return item.source
        return self.provenance_default

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        keys = [item.parameter for item in self.provenance]
        if len(set(keys)) != len(keys):
            raise ValueError("Parameter provenance must be unique")
        return self


class UsageOperationScope(UsageModel):
    """Exact resource semantics, not a provider-name or endpoint classification."""

    version: Literal[1] = 1
    resource: Literal["openai.embeddings.create", "openai.images.generate"]
    abi_revision: Literal[1] = 1

    @property
    def operation(self) -> Literal["embeddings", "native"]:
        """Physical capture operation compatible with this resource."""
        return "embeddings" if self.resource == "openai.embeddings.create" else "native"


class UsageServiceTierApplicability(UsageModel):
    """Observed semantic applicability; the serialized value is not authority."""

    version: Literal[1] = 1
    state: Literal["unknown", "applicable", "not_applicable"]
    scope: UsageOperationScope | None = None

    @model_validator(mode="after")
    def scoped_evidence(self) -> Self:
        if (self.state == "not_applicable") != (self.scope is not None):
            raise ValueError("Only operation-scoped not-applicable evidence carries a scope")
        return self


class UsageUntieredTariffDeclaration(UsageModel):
    """Explicit aggregate-token estimate; never an inferred native media tariff."""

    version: Literal[1] = 1
    scope: UsageOperationScope
    billing_basis: Literal["aggregate_input_tokens", "aggregate_input_output_tokens"]

    @model_validator(mode="after")
    def supported_units(self) -> Self:
        expected = (
            "aggregate_input_tokens"
            if self.scope.resource == "openai.embeddings.create"
            else "aggregate_input_output_tokens"
        )
        if self.billing_basis != expected:
            raise ValueError("Aggregate tariff billing basis does not match its operation")
        return self


class UsageModelSelection(UsageModel):
    """Resolved routing metadata; no credential or request payload fields."""

    provider_id: UsageId
    effective_model: UsageModelName
    requested_model: UsageModelName | None = None
    requested_route: UsageId | None = None
    requested_effort: UsageLabel | None = None
    effective_effort: UsageLabel | None = None
    reasoning_budget: UsageInteger | None = None
    service_tier: UsageLabel | None = None
    service_tier_applicability: UsageServiceTierApplicability | None = None
    cache_ttl: Literal["5m", "1h"] | None = None
    streaming: bool
    source: UsageSource
    operation: UsageOperation
    captured_price_revision_id: UsageId | None = None
    resolver_snapshot_id: UsageId | None = None
    requested_parameters: UsageModelParameters = Field(default_factory=UsageModelParameters)
    effective_parameters: UsageModelParameters = Field(default_factory=UsageModelParameters)


class UsageAttribution(UsageModel):
    """One trusted attribution bundle; unverified client labels stay separate."""

    identity: UsageIdentity
    purpose: UsageLabel
    resources: UsageResourceBinding | None = None
    hook: UsageHookOrigin | None = None
    client_labels: UsageClientLabels | None = None


class UsageExecutorContext(UsageModel):
    """Immutable correlation across executor dispatch; not a durable receipt."""

    executor_id: UsageId
    controller_owner_id: UsageId
    inference_request_id: UsageId
    connection_epoch: UsageInteger | None = None


class UsageMetadataReduction(UsageModel):
    """One explicit immutable reduction decision; verified resources are never reduced."""

    reason: Literal["envelope_budget"] = "envelope_budget"
    fields: Annotated[
        tuple[
            Literal[
                "client_labels",
                "hook_display_name",
                "requested_parameter_provenance",
                "effective_parameter_provenance",
                "requested_parameters",
                "effective_parameters",
            ],
            ...,
        ],
        Field(min_length=1, max_length=6),
    ]

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("Reduced metadata fields must be unique")
        return self


class UsageAttemptContext(UsageModel):
    """Repeat in terminal observations so a lost start does not lose attribution."""

    trace: UsageTrace
    attribution: UsageAttribution
    selection: UsageModelSelection
    started_at: UsageTimestamp
    request_started_at: UsageTimestamp | None = None
    controller_owner_id: UsageId
    coverage_epoch_id: UsageId
    executor: UsageExecutorContext | None = None
    metadata_reduction: UsageMetadataReduction | None = None


class UsageCount(UsageModel):
    """An absent count is not reported zero; partial counts remain identifiable."""

    value: UsageInteger | None = None
    status: UsageCoverage = "unknown"
    source: UsageSource | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if (self.status in ("reported", "partial")) != (self.value is not None):
            raise ValueError("Reported/partial counts require a value; unknown counts forbid one")
        if self.value is not None and self.source is None:
            raise ValueError("Observed counts require semantic provenance")
        return self


class UsageNumericExtra(UsageModel):
    """Only this small numeric vocabulary is retained beyond normalized counts."""

    name: Literal[
        "input_audio_tokens",
        "output_audio_tokens",
        "input_image_tokens",
        "output_image_tokens",
        "input_text_tokens",
        "output_text_tokens",
        "cache_read_image_tokens",
        "cache_read_audio_tokens",
        "input_audio_duration_ms",
        "output_audio_duration_ms",
        "input_video_duration_ms",
        "output_video_duration_ms",
        "input_image_count",
        "output_image_count",
        "input_characters",
        "output_characters",
        "billed_characters",
    ]
    value: UsageInteger
    unit: Literal["tokens", "milliseconds", "images", "characters"] | None = None
    status: Literal["reported", "derived", "partial"] = "reported"
    source: UsageSource | None = None

    @property
    def effective_unit(self) -> str:
        """Legacy token extras remain unambiguous without an explicit unit field."""
        if self.name.endswith("_tokens"):
            return "tokens"
        if self.name.endswith("_duration_ms"):
            return "milliseconds"
        if self.name.endswith("_image_count"):
            return "images"
        return "characters"

    @model_validator(mode="after")
    def validate_unit(self) -> Self:
        if self.unit is None and not self.name.endswith("_tokens"):
            raise ValueError("Native modality counts require an explicit unit")
        if self.unit is not None and self.unit != self.effective_unit:
            raise ValueError("Numeric usage unit does not match its typed name")
        return self


class UsageProviderCost(UsageModel):
    """Provider-reported monetary evidence, not a catalog estimate or reservation."""

    amount: Annotated[Decimal, Field(ge=0, max_digits=30, decimal_places=12, allow_inf_nan=False)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "USD"
    source: Literal["provider"] = "provider"


class NormalizedUsage(UsageModel):
    """Normalized counts with explicit inclusive/additive cache/reasoning rules."""

    normalization_version: Literal[1] = 1
    semantic_profile: UsageLabel
    semantic_version: Annotated[StrictInt, Field(ge=1)]
    completeness: UsageCoverage = "unknown"
    input_tokens: UsageCount = Field(default_factory=UsageCount)
    output_tokens: UsageCount = Field(default_factory=UsageCount)
    provider_total_tokens: UsageCount = Field(default_factory=UsageCount)
    cache_read_tokens: UsageCount = Field(default_factory=UsageCount)
    cache_write_tokens: UsageCount = Field(default_factory=UsageCount)
    cache_write_5m_tokens: UsageCount = Field(default_factory=UsageCount)
    cache_write_1h_tokens: UsageCount = Field(default_factory=UsageCount)
    reasoning_tokens: UsageCount = Field(default_factory=UsageCount)
    cache_read_semantics: Literal["included_in_input", "additional_to_input", "unknown"]
    cache_write_semantics: Literal["included_in_input", "additional_to_input", "unknown"]
    reasoning_semantics: Literal["included_in_output", "additional_to_output", "unknown"]
    cache_eligible: bool | None = None
    gateway_response_cache_hit: bool | None = None
    extras: Annotated[tuple[UsageNumericExtra, ...], Field(max_length=20)] = ()
    provider_cost: UsageProviderCost | None = None

    @model_validator(mode="after")
    def validate_extras(self) -> Self:
        if len({item.name for item in self.extras}) != len(self.extras):
            raise ValueError("Numeric usage fields must be unique")
        return self


class UsagePerformance(UsageModel):
    """TTFT means first meaningful generated response, never headers/keepalives."""

    started_at: UsageTimestamp | None = None
    ended_at: UsageTimestamp | None = None
    total_elapsed_ms: UsageMilliseconds | None = None
    dispatch_elapsed_ms: UsageMilliseconds | None = None
    provider_elapsed_ms: UsageMilliseconds | None = None
    ttft_ms: UsageMilliseconds | None = None
    first_generated_at: UsageTimestamp | None = None
    last_generated_at: UsageTimestamp | None = None
    generation_elapsed_ms: UsageMilliseconds | None = None
    source: UsageSource
    clock_domain: Literal["controller", "executor", "provider"] = "controller"

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        for start, end in (
            (self.started_at, self.ended_at),
            (self.first_generated_at, self.last_generated_at),
        ):
            if start is not None and end is not None and end < start:
                raise ValueError("Observed end cannot precede observed start")
        return self


class UsageSafeError(UsageModel):
    """A template key, not an exception message; adapters map known errors."""

    category: Literal[
        "timeout",
        "rate_limit",
        "authentication",
        "permission",
        "invalid_request",
        "context_limit",
        "provider_unavailable",
        "transport",
        "cancelled",
        "unknown",
    ]
    summary_code: Literal[
        "provider_timeout",
        "provider_rate_limited",
        "provider_authentication_failed",
        "provider_permission_denied",
        "provider_request_rejected",
        "context_limit_exceeded",
        "provider_unavailable",
        "transport_failed",
        "request_cancelled",
        "provider_error",
    ]
    exception_class: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")] | None
    ) = None
    provider_error_code: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")] | None
    ) = None
    http_status: Annotated[StrictInt, Field(ge=100, le=599)] | None = None


class UsageEnvelope(UsageModel):
    """Each queued observation has its own hard encoded-byte ceiling."""

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        if (
            len(self.model_dump_json(exclude_none=True, exclude_defaults=True).encode())
            > USAGE_OBSERVATION_MAX_BYTES
        ):
            raise ValueError("Usage observation exceeds 8 KiB")
        return self


class UsageAttemptStarted(UsageEnvelope):
    """Best-effort start observation. Emission never waits for persistence."""

    schema_version: Literal[1] = 1
    kind: Literal["attempt_started"] = "attempt_started"
    observation_id: UsageId
    context: UsageAttemptContext


class UsageAttemptFinalized(UsageEnvelope):
    """One terminal observation per attempt; DB retry must never repeat inference."""

    schema_version: Literal[1] = 1
    kind: Literal["attempt_finalized"] = "attempt_finalized"
    context: UsageAttemptContext
    observation_id: UsageId
    outcome: UsageOutcome
    dispatch_status: Literal["not_dispatched", "dispatch_observed", "provider_accepted", "unknown"]
    finalized_at: UsageTimestamp
    performance: UsagePerformance
    usage: NormalizedUsage
    provider_request_id: UsageId | None = None
    provider_reported_model: UsageModelName | None = None
    error: UsageSafeError | None = None


class UsageServerSpan(UsageModel):
    """Server-local logical request timings; sent is never external received."""

    received_at: UsageTimestamp | None = None
    first_generated_at: UsageTimestamp | None = None
    first_sent_at: UsageTimestamp | None = None
    last_sent_at: UsageTimestamp | None = None
    completed_at: UsageTimestamp | None = None
    elapsed_ms: UsageMilliseconds | None = None
    ttft_ms: UsageMilliseconds | None = None
    send_elapsed_ms: UsageMilliseconds | None = None
    clock_domain: Literal["controller", "executor"] = "controller"


class UsageClientSpan(UsageModel):
    """Client-reported clock domain; never subtract from server/provider clocks."""

    source: Literal["authenticated_report", "unverified_report"]
    sent_at: UsageTimestamp | None = None
    first_received_at: UsageTimestamp | None = None
    completed_at: UsageTimestamp | None = None
    elapsed_ms: UsageMilliseconds | None = None
    ttft_ms: UsageMilliseconds | None = None


class UsageHTTPMetadata(UsageModel):
    """Allowlisted private request metadata; never arbitrary headers or content."""

    request_path: Literal["/v1/chat/completions", "/v1/embeddings", "/v1/systemone"]
    streaming: bool | None = None
    user_agent: (
        Annotated[str, StringConstraints(max_length=256, pattern=r"^[\x20-\x7e]*$")] | None
    ) = None
    app_name: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")] | None
    ) = None
    app_version: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")] | None
    ) = None
    http_status_code: Annotated[StrictInt, Field(ge=100, le=599)] | None = None
    error: UsageSafeError | None = None
    finish_reason: (
        Literal["stop", "length", "tool_calls", "function_call", "content_filter", "other"] | None
    ) = None


class UsageRequestStats(UsageEnvelope):
    """Content-free logical request outcome/spans, not another physical attempt."""

    schema_version: Literal[1] = 1
    kind: Literal["request_stats"] = "request_stats"
    observation_id: UsageId
    request_id: UsageId
    request_started_at: UsageTimestamp
    observed_at: UsageTimestamp
    attribution: UsageAttribution
    controller_owner_id: UsageId
    coverage_epoch_id: UsageId
    stats_scope: Literal["server", "client"]
    cycle_id: UsageId | None = None
    attempt_id: UsageId | None = None
    logical_outcome: UsageOutcome | None = None
    server: UsageServerSpan | None = None
    client: UsageClientSpan | None = None
    error: UsageSafeError | None = None
    metadata_reduction: UsageMetadataReduction | None = None
    http_metadata: UsageHTTPMetadata | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.stats_scope != "server" and self.http_metadata is not None:
            raise ValueError("Only the server may record HTTP metadata")
        if self.http_metadata is not None:
            size = len(self.http_metadata.model_dump_json(exclude_none=True).encode())
            if self.attribution.client_labels is not None:
                size += len(
                    self.attribution.client_labels.model_dump_json(exclude_none=True).encode()
                )
            if size > 2048:
                raise ValueError("Private HTTP metadata exceeds 2 KiB")
        if self.stats_scope == "server" and (self.server is None or self.client is not None):
            raise ValueError("Server statistics require only a server-local span")
        if self.stats_scope == "client" and (
            self.client is None or self.server is not None or self.logical_outcome is not None
        ):
            raise ValueError("Client reports cannot replace the server logical outcome")
        return self


class UsageCorrection(UsageEnvelope):
    """Explicit audited replacement, never an overwrite through replay."""

    correction_id: UsageId
    expected_fact_version: Annotated[StrictInt, Field(ge=1)]
    reason: Literal["late_provider_usage", "normalization_fix", "authoritative_reconciliation"]
    replacement: UsageAttemptFinalized


class UsagePriceRate(UsageModel):
    """Immutable decimal rate for a declared billable unit."""

    dimension: Literal[
        "input",
        "output",
        "cache_read",
        "cache_write",
        "cache_write_5m",
        "cache_write_1h",
        "reasoning",
        "input_audio_tokens",
        "output_audio_tokens",
        "input_image_tokens",
        "output_image_tokens",
        "input_text_tokens",
        "output_text_tokens",
        "cache_read_image_tokens",
        "cache_read_audio_tokens",
        "input_audio_duration_ms",
        "output_audio_duration_ms",
        "output_video_duration_ms",
        "input_image_count",
        "output_image_count",
        "input_characters",
        "output_characters",
        "billed_characters",
    ]
    per_million: Annotated[Decimal, Field(ge=0, max_digits=30, decimal_places=12)]
    service_tier: UsageLabel | None = None
    cache_ttl: Literal["5m", "1h"] | None = None
    input_threshold: UsageInteger = 0
    image_size: UsageLabel | None = None
    image_quality: UsageLabel | None = None


class UsagePriceDefinition(UsageModel):
    """Published on configuration/metadata change, never once per attempt."""

    revision_id: UsageId
    provider_id: UsageId
    model: UsageModelName
    untiered_tariff: UsageUntieredTariffDeclaration | None = None
    media_operations: tuple[UsageMediaOperation, ...] | None = None
    effective_at: UsageTimestamp
    recorded_at: UsageTimestamp
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    basis: UsageCostBasis
    provenance: Literal["configured", "provider_metadata", "catalog"]
    source_revision: UsageId | None = None
    rule_version: Literal[1, 2, 3] = 1
    rates: Annotated[tuple[UsagePriceRate, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def validate_rate_keys(self) -> Self:
        if self.media_operations:
            if self.rule_version != 3 or self.untiered_tariff is not None:
                raise ValueError("Media tariffs require their own rule")
            if len(set(self.media_operations)) != len(self.media_operations):
                raise ValueError("Media tariff operations must be unique")
            if any(r.service_tier is not None or r.input_threshold for r in self.rates):
                raise ValueError("Media tariffs cannot infer service or context tiers")
        if self.untiered_tariff is not None:
            if (
                self.rule_version != 2
                or self.provenance != "configured"
                or self.basis != "configured_estimate"
            ):
                raise ValueError("Untiered tariffs require the configured aggregate rule")
            if any(
                rate.service_tier is not None
                or rate.cache_ttl is not None
                or rate.input_threshold != 0
                or rate.dimension not in {"input", "output"}
                for rate in self.rates
            ):
                raise ValueError("Aggregate tariffs cannot mix tier, cache or context rules")
            expected = (
                {"input"}
                if self.untiered_tariff.billing_basis == "aggregate_input_tokens"
                else {"input", "output"}
            )
            if {rate.dimension for rate in self.rates} != expected:
                raise ValueError("Aggregate tariffs require every declared scalar rate")
        elif not self.media_operations and self.rule_version != 1:
            raise ValueError("Aggregate rule requires an explicit untiered tariff")
        if self.basis == "provider_reported":
            raise ValueError("Provider-reported charges are observations, not tariffs")
        keys = [
            (
                r.dimension,
                r.service_tier,
                r.cache_ttl,
                r.input_threshold,
                r.image_size,
                r.image_quality,
            )
            for r in self.rates
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("Price rules must have unique rate keys")
        return self


class UsageAttemptSink(Protocol):
    """Nonblocking best-effort boundary; no DB I/O, capacity wait or inference error."""

    def try_emit(self, observation: UsageObservation) -> bool:
        """Return in-memory acceptance only; false records a bounded coverage gap."""
        ...


UsageObservation = Annotated[
    UsageAttemptStarted | UsageAttemptFinalized | UsageRequestStats,
    Field(discriminator="kind"),
]
