"""Public usage query contracts, independent of persistence and HTTP."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from cognis.models.usage import (
    UsageClientLabels,
    UsageDisplayLabel,
    UsageModelName,
    UsageNumericExtra,
    UsageOperation,
    UsageServiceTierApplicability,
)

MAX_SAFE_INTEGER = 2**53 - 1
Count = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
DecimalString = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=100)]
Identifier = Annotated[str, Field(min_length=1, max_length=254, pattern=r"^[^\s\x00-\x1f]+$")]
FilterValues = Annotated[list[Identifier], Field(min_length=1, max_length=100)]
UsageMode = Literal["personal", "project", "admin"]
AttemptState = Literal["observed_start", "inflight", "succeeded", "failed", "cancelled", "unknown"]
ErrorCategory = Literal[
    "authentication",
    "rate_limit",
    "timeout",
    "connection",
    "cancelled",
    "invalid_request",
    "provider",
    "internal",
    "unknown",
]
UsageQuality = Literal["complete", "partial", "unknown"]
CostBasis = Literal["api_estimate", "configured_equivalent", "provider_reported"]
UsageDimension = Literal[
    "subject",
    "agent",
    "profile",
    "provider",
    "model",
    "effort",
    "purpose",
    "hook",
    "service",
    "operation",
    "project",
    "conversation",
    "session",
    "traffic_origin",
    "auth_type",
    "client",
    "key",
    "task",
    "schedule",
]
ForecastReason = Literal[
    "insufficient",
    "sparse",
    "partial",
    "mixed_currency",
    "mixed_basis",
    "unpriced",
    "unknown",
    "overflow",
    "capture_gap",
    "capture_unknown",
    "projection_lag",
    "unsupported_resolution",
]


class UsageModel(BaseModel):
    """Strict wire contract; arbitrary query fields are never accepted."""

    model_config = ConfigDict(extra="forbid", validate_default=True)


FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]
ParameterProvenance = Literal[
    "explicit",
    "configured_default",
    "provider_default",
    "adapter_normalized",
    "observed_unspecified",
    "unsupported",
    "unknown",
]


class UsageParameter[ParameterType](UsageModel):
    """One allowlisted setting; provenance never claims the provider honored it."""

    value: ParameterType | None = None
    provenance: ParameterProvenance = "unknown"

    @model_validator(mode="after")
    def known_value(self) -> Self:
        if self.provenance in {"unknown", "unsupported"} and self.value is not None:
            raise ValueError("Unknown/unsupported parameters cannot claim an effective value")
        if (
            self.provenance in {"explicit", "configured_default", "adapter_normalized"}
            and self.value is None
        ):
            raise ValueError("Known parameters require a typed value")
        return self


class UsageParameterSnapshot(UsageModel):
    """Versioned content-free envelope; no stop text, tool names or JSON schemas."""

    version: Literal[1] = 1
    temperature: UsageParameter[Annotated[FiniteNumber, Field(ge=0)]] = Field(
        default_factory=UsageParameter
    )
    top_p: UsageParameter[Annotated[FiniteNumber, Field(ge=0, le=1)]] = Field(
        default_factory=UsageParameter
    )
    top_k: UsageParameter[Count] = Field(default_factory=UsageParameter)
    max_tokens: UsageParameter[Count] = Field(default_factory=UsageParameter)
    max_output_tokens: UsageParameter[Count] = Field(default_factory=UsageParameter)
    max_completion_tokens: UsageParameter[Count] = Field(default_factory=UsageParameter)
    max_input_tokens: UsageParameter[Count] = Field(default_factory=UsageParameter)
    max_total_tokens: UsageParameter[Count] = Field(default_factory=UsageParameter)
    seed: UsageParameter[
        Annotated[int, Field(strict=True, ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]
    ] = Field(default_factory=UsageParameter)
    frequency_penalty: UsageParameter[FiniteNumber] = Field(default_factory=UsageParameter)
    presence_penalty: UsageParameter[FiniteNumber] = Field(default_factory=UsageParameter)
    repetition_penalty: UsageParameter[Annotated[FiniteNumber, Field(ge=0)]] = Field(
        default_factory=UsageParameter
    )
    reasoning_effort: UsageParameter[
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]
    ] = Field(default_factory=UsageParameter)
    reasoning_budget_tokens: UsageParameter[Count] = Field(default_factory=UsageParameter)
    reasoning_enabled: UsageParameter[bool] = Field(default_factory=UsageParameter)
    service_tier: UsageParameter[
        Literal["auto", "default", "standard", "priority", "flex", "scale", "batch"]
    ] = Field(default_factory=UsageParameter)
    response_format: UsageParameter[Literal["text", "json_object", "json_schema"]] = Field(
        default_factory=UsageParameter
    )
    tool_choice: UsageParameter[Literal["auto", "none", "required", "named"]] = Field(
        default_factory=UsageParameter
    )


class UsageRequestParameters(UsageModel):
    """Requested settings and adapter-observed effective settings stay distinct."""

    requested: UsageParameterSnapshot = Field(default_factory=UsageParameterSnapshot)
    effective: UsageParameterSnapshot = Field(default_factory=UsageParameterSnapshot)
    service_tier_applicability: UsageServiceTierApplicability | None = None


class UsageServerSpan(UsageModel):
    """Offsets on one controller-local logical request clock, never remote clocks."""

    kind: Literal[
        "routing",
        "queue",
        "provider_wait",
        "executor_wait",
        "accounting_hook",
        "finalization",
        "delivery",
        "other_cognis",
    ]
    start_seconds: Seconds
    end_seconds: Seconds

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("Server span end precedes its start")
        return self


class UsageServerTiming(UsageModel):
    """Logical server boundary, not provider latency or proof of client receipt."""

    source: Literal["controller_monotonic"] = "controller_monotonic"
    definition: Literal["exclusive_controller_spans_v1"] = "exclusive_controller_spans_v1"
    elapsed_seconds: Seconds | None = None
    send_duration_seconds: Seconds | None = None
    first_send_seconds: Seconds | None = None
    last_send_seconds: Seconds | None = None
    spans: Annotated[list[UsageServerSpan], Field(max_length=64)] = Field(default_factory=list)
    partition_complete: bool = False

    @model_validator(mode="after")
    def bounded_partition(self) -> Self:
        if self.elapsed_seconds is None:
            if (
                self.partition_complete
                or self.spans
                or self.first_send_seconds is not None
                or self.last_send_seconds is not None
            ):
                raise ValueError("Server observations require a measured logical boundary")
            return self
        for offset in (self.first_send_seconds, self.last_send_seconds):
            if offset is not None and offset > self.elapsed_seconds:
                raise ValueError("Server send exceeds the measured logical boundary")
        if (
            self.first_send_seconds is not None
            and self.last_send_seconds is not None
            and self.first_send_seconds > self.last_send_seconds
        ):
            raise ValueError("Server first send follows last send")
        ordered = sorted(self.spans, key=lambda span: span.start_seconds)
        for span in ordered:
            if span.end_seconds > self.elapsed_seconds:
                raise ValueError("Server span exceeds the measured logical boundary")
        if self.partition_complete:
            cursor = 0.0
            for span in ordered:
                if span.start_seconds != cursor:
                    raise ValueError("Complete partition must be contiguous and non-overlapping")
                cursor = span.end_seconds
            if cursor != self.elapsed_seconds:
                raise ValueError("Complete partition must cover the logical boundary")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cognis_overhead_seconds(self) -> float | None:
        """Only an exclusive partition can establish measured Cognis overhead.

        An unresolved executor wait may include remote Cognis work. It makes the
        total overhead unavailable rather than guessing from another host's clock.
        """
        if not self.partition_complete or any(span.kind == "executor_wait" for span in self.spans):
            return None
        return sum(
            span.end_seconds - span.start_seconds
            for span in self.spans
            if span.kind != "provider_wait"
        )


class UsageClientTiming(UsageModel):
    """Authenticated correlated client-local elapsed observations, not authority."""

    source: Literal["cognis_ui", "authenticated_client_report"]
    clock: Literal["client_monotonic"] = "client_monotonic"
    correlation_verified: Literal[True]
    first_chunk_seconds: Annotated[Seconds, Field(le=86400)] | None = None
    last_chunk_seconds: Annotated[Seconds, Field(le=86400)] | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            self.first_chunk_seconds is not None
            and self.last_chunk_seconds is not None
            and self.first_chunk_seconds > self.last_chunk_seconds
        ):
            raise ValueError("Client first chunk follows last chunk")
        return self


class UsageLogicalTiming(UsageModel):
    """Reported per logical request, never summed once per retry/attempt."""

    server: UsageServerTiming | None = None
    client: UsageClientTiming | None = None


class UsageCaptureCoverage(UsageModel):
    """Best-effort observed coverage; even no detected gap is not crash-proof."""

    strategy: Literal["asynchronous_best_effort"] = "asynchronous_best_effort"
    state: Literal["observed", "gaps", "unknown"] = "unknown"
    epoch_started_at: AwareDatetime | None = None
    dropped_observations: Count | None = None
    gap_incidents: Count | None = None
    hard_crash_loss_possible: Literal[True] = True

    @model_validator(mode="after")
    def explicit_gaps(self) -> Self:
        if self.state == "observed" and (self.dropped_observations or self.gap_incidents):
            raise ValueError("Known capture losses require gaps state")
        return self


class UsageProjectionCoverage(UsageModel):
    """Projection progress is independent of asynchronous capture losses."""

    state: Literal["ready", "lagging", "building", "unavailable"] = "unavailable"
    captured_through: AwareDatetime | None = None
    projected_through: AwareDatetime | None = None
    lag_seconds: Seconds | None = None
    unprojected_observations: Count | None = None

    @model_validator(mode="after")
    def ready_is_not_lagging(self) -> Self:
        if self.state == "ready" and (
            self.unprojected_observations or (self.lag_seconds is not None and self.lag_seconds > 0)
        ):
            raise ValueError("Ready projection cannot have known outstanding work")
        return self


class UsageAvailability(UsageModel):
    """Authorized source resolution and retention, never inferred from empty rows."""

    resolution: Literal["retained", "aggregate_only", "detail_expired", "mixed", "unavailable"] = (
        "unavailable"
    )
    detail_retention_cutoff: AwareDatetime | None = None
    configured_detail_days: Count | None = None
    archive_grain: Literal["day"] | None = None
    supported_dimensions: Annotated[list[UsageDimension], Field(max_length=32)] = Field(
        default_factory=list
    )
    unsupported_fields: Annotated[
        list[
            Literal[
                "parameters",
                "request_ids",
                "cycle_ids",
                "turn_ids",
                "client_ids",
                "key_ids",
                "provider_request_ids",
                "profile",
                "subday_time",
                "attempt_detail",
            ]
        ],
        Field(max_length=11),
    ] = Field(default_factory=list)
    historical_repricing: Literal["supported", "unsupported", "unknown"] = "unknown"
    repricing_unavailable_reason: (
        Literal["missing_billing_strata", "unsupported_per_request_rules", "unknown"] | None
    ) = None
    stored_valuation_preserved: Literal[True] = True

    @model_validator(mode="after")
    def explicit_expiry(self) -> Self:
        if self.resolution in {"aggregate_only", "detail_expired", "mixed"} and (
            self.detail_retention_cutoff is None or self.archive_grain is None
        ):
            raise ValueError("Expired detail requires an explicit cutoff and archive grain")
        if self.historical_repricing == "supported" and self.repricing_unavailable_reason:
            raise ValueError("Supported repricing cannot have an unavailable reason")
        return self


class UsageScope(UsageModel):
    """Requested visibility, not an authorization grant."""

    mode: UsageMode = "personal"
    project_id: Identifier | None = None

    @model_validator(mode="after")
    def valid_project(self) -> Self:
        if self.mode == "project" and self.project_id is None:
            raise ValueError("Project mode requires project_id")
        if self.mode == "admin" and self.project_id is not None:
            raise ValueError("Use project filters rather than an admin project scope")
        return self


class UsageConversationTree(UsageModel):
    """Conversation execution membership, using the sidebar inclusion semantics."""

    id: Identifier
    inclusion: Literal["direct", "inclusive"]


class UsageFilters(UsageModel):
    """Bounded filters; the query catalog additionally checks source compatibility."""

    subject_emails: FilterValues | None = None
    subject_keys: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=333)]],
            Field(min_length=1, max_length=100),
        ]
        | None
    ) = None
    provider_owner_emails: FilterValues | None = None
    provider_ids: FilterValues | None = None
    model_ids: Annotated[list[UsageModelName], Field(min_length=1, max_length=100)] | None = None
    agent_ids: FilterValues | None = None
    # "system": requests made by Cognis itself and companion services, without an agent.
    agent_scope: Literal["system"] | None = None
    profile_ids: FilterValues | None = None
    efforts: FilterValues | None = None
    purposes: FilterValues | None = None
    hook_ids: FilterValues | None = None
    service_ids: FilterValues | None = None
    operations: FilterValues | None = None
    project_ids: FilterValues | None = None
    conversation_ids: FilterValues | None = None
    conversation_tree: UsageConversationTree | None = None
    session_ids: FilterValues | None = None
    task_ids: FilterValues | None = None
    schedule_ids: FilterValues | None = None
    task_origin: Literal["scheduled", "one_off"] | None = None
    step_run_ids: FilterValues | None = None
    turn_ids: FilterValues | None = None
    cycle_ids: FilterValues | None = None
    key_ids: FilterValues | None = None
    client_ids: FilterValues | None = None
    traffic_origins: (
        Annotated[list[Literal["cognis", "api", "unknown"]], Field(min_length=1, max_length=3)]
        | None
    ) = None
    auth_types: (
        Annotated[
            list[Literal["api_key", "user_jwt", "service", "browser_session", "unknown"]],
            Field(min_length=1, max_length=5),
        ]
        | None
    ) = None
    http_status_codes: (
        Annotated[
            list[Annotated[int, Field(strict=True, ge=100, le=599)]],
            Field(min_length=1, max_length=100),
        ]
        | None
    ) = None
    logical_request_ids: FilterValues | None = None
    attempt_ids: FilterValues | None = None
    error_categories: Annotated[list[ErrorCategory], Field(max_length=100)] | None = None
    states: Annotated[list[AttemptState], Field(min_length=1, max_length=6)] | None = None


class ForecastOptions(UsageModel):
    """Extrapolation horizon, independent of the selected sample window."""

    horizon_seconds: Annotated[int, Field(strict=True, ge=1, le=30 * 86400)]


class UsageQuery(UsageModel):
    """Catalog-driven analytics request, also the read-only tool argument model."""

    scope: UsageScope = Field(default_factory=UsageScope)
    start: AwareDatetime
    end: AwareDatetime
    view: Literal["overview", "ranking", "trend", "companions", "cost_breakdowns"] = "overview"
    filters: UsageFilters = Field(default_factory=UsageFilters)
    group_by: UsageDimension | None = None
    bucket: Literal["hour", "day"] = "day"
    limit: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100
    pricing_basis: Literal["historical", "current"] = "historical"
    metric: Literal["attempts", "cost", "input", "output"] = "cost"
    include_pending: bool = False
    forecast: ForecastOptions | None = None

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        self.start = self.start.astimezone(UTC)
        self.end = self.end.astimezone(UTC)
        maximum = timedelta(days=90 if self.bucket == "hour" else 730)
        if not timedelta(0) < self.end - self.start <= maximum:
            raise ValueError("Window must be positive and within the selected bucket budget")
        if self.view == "ranking" and self.group_by is None:
            raise ValueError("Ranking requires group_by")
        if self.filters.conversation_tree and self.view not in {"overview", "ranking", "trend"}:
            raise ValueError("Conversation trees support overview, ranking and trend only")
        if self.view == "cost_breakdowns" and (
            self.group_by is not None or self.forecast is not None or self.metric != "cost"
        ):
            raise ValueError("Cost breakdowns have fixed dimensions/measure and no forecast")
        return self


class UsageAttemptQuery(UsageModel):
    """Bounded keyset detail request; cursors never carry authority."""

    scope: UsageScope = Field(default_factory=UsageScope)
    start: AwareDatetime
    end: AwareDatetime
    filters: UsageFilters = Field(default_factory=UsageFilters)
    limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100
    cursor: Annotated[str, Field(max_length=4096)] | None = None
    pricing_basis: Literal["historical", "current"] = "historical"
    selection_ref: Annotated[str, Field(max_length=4096)] | None = None

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if not timedelta(0) < self.end - self.start <= timedelta(days=90):
            raise ValueError("Attempt detail requires a positive window of at most 90 days")
        return self


class UsageRequestQuery(UsageAttemptQuery):
    """Opt-in unique-request paging preserves legacy physical-attempt consumers."""

    pagination_unit: Literal["attempt", "request"] = "attempt"


class UsageRequestDetailQuery(UsageAttemptQuery):
    """An opaque selection narrows fresh authority, never a naked request ID."""

    selection_ref: Annotated[str, Field(min_length=1, max_length=4096)]


class TokenTotals(UsageModel):
    """Known subtotals; null means no supported observation, not zero."""

    input: Count | None = None
    output: Count | None = None
    cache_read: Count | None = None
    cache_write: Count | None = None
    reasoning: Count | None = None


class TokenCoverage(UsageModel):
    """Known observation counts per token field, including explicit zero."""

    input: Count = 0
    output: Count = 0
    cache_read: Count = 0
    cache_write: Count = 0
    reasoning: Count = 0


class CostSubtotal(UsageModel):
    """Exact known estimate, separated by currency and billing basis."""

    known_amount: DecimalString | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    basis: CostBasis
    quality: UsageQuality
    priced_attempts: Count
    unpriced_attempts: Count
    partially_priced_attempts: Count = 0


class UsageDurationStats(UsageModel):
    """One measured population on a defined boundary, never inferred by subtraction."""

    known_samples: Count = 0
    seconds_sum: Seconds | None = None

    @model_validator(mode="after")
    def matched_population(self) -> Self:
        if (self.known_samples == 0) != (self.seconds_sum is None):
            raise ValueError("Timing sum requires a matching known population")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def seconds_mean(self) -> float | None:
        return (
            self.seconds_sum / self.known_samples
            if self.known_samples and self.seconds_sum is not None
            else None
        )


class UsageClientPerformance(UsageModel):
    """Client-local measured first/last-chunk populations, not server-send estimates."""

    first_chunk: UsageDurationStats = Field(default_factory=UsageDurationStats)
    last_chunk: UsageDurationStats = Field(default_factory=UsageDurationStats)


class UsageLogicalPerformance(UsageModel):
    """Logical request statistics contributed once, not once for each retry."""

    server_boundary: UsageDurationStats = Field(default_factory=UsageDurationStats)
    server_first_send: UsageDurationStats = Field(default_factory=UsageDurationStats)
    server_last_send: UsageDurationStats = Field(default_factory=UsageDurationStats)
    cognis_overhead: UsageDurationStats = Field(default_factory=UsageDurationStats)
    routing: UsageDurationStats = Field(default_factory=UsageDurationStats)
    queue: UsageDurationStats = Field(default_factory=UsageDurationStats)
    provider_wait: UsageDurationStats = Field(default_factory=UsageDurationStats)
    executor_wait: UsageDurationStats = Field(default_factory=UsageDurationStats)
    accounting_hook: UsageDurationStats = Field(default_factory=UsageDurationStats)
    finalization: UsageDurationStats = Field(default_factory=UsageDurationStats)
    delivery: UsageDurationStats = Field(default_factory=UsageDurationStats)
    cognis_ui: UsageClientPerformance = Field(default_factory=UsageClientPerformance)
    authenticated_client: UsageClientPerformance = Field(default_factory=UsageClientPerformance)


class UsagePerformance(UsageModel):
    """Provider-attempt statistics; end_to_end means provider dispatch-to-finish.

    It is not logical-server or client-observed E2E. Those populations live in
    UsageLogicalPerformance and are never inferred from these provider values.
    """

    ttft_known_samples: Count = 0
    ttft_seconds_sum: Seconds | None = None
    latency_known_samples: Count = 0
    latency_seconds_sum: Seconds | None = None
    generation_known_samples: Count = 0
    generation_seconds_sum: Seconds | None = None
    generation_output_tokens: Count | None = None
    end_to_end_known_samples: Count = 0
    end_to_end_seconds_sum: Seconds | None = None
    end_to_end_output_tokens: Count | None = None
    dispatched_attempts: Count | None = None
    failed_dispatched_attempts: Count | None = None
    retry_attempts: Count | None = None
    fallback_attempts: Count | None = None
    logical_completed_requests: Count | None = None
    logical_failed_requests: Count | None = None

    @model_validator(mode="after")
    def valid_populations(self) -> Self:
        for prefix in ("ttft", "latency", "generation", "end_to_end"):
            count = getattr(self, f"{prefix}_known_samples")
            total = getattr(self, f"{prefix}_seconds_sum")
            if (count == 0) != (total is None):
                raise ValueError(f"{prefix} sum must have a matching known sample population")
        for prefix in ("generation", "end_to_end"):
            if (getattr(self, f"{prefix}_known_samples") == 0) != (
                getattr(self, f"{prefix}_output_tokens") is None
            ):
                raise ValueError(f"{prefix} tokens must have a matching measured population")
        if self.failed_dispatched_attempts is not None and self.dispatched_attempts is None:
            raise ValueError("Failed dispatched attempts require a dispatch denominator")
        if (
            self.failed_dispatched_attempts is not None
            and self.dispatched_attempts is not None
            and self.failed_dispatched_attempts > self.dispatched_attempts
        ):
            raise ValueError("Failed dispatched attempts exceed dispatched attempts")
        if (self.logical_completed_requests is None) != (self.logical_failed_requests is None):
            raise ValueError("Logical rates require trusted completion and failure counts")
        if (
            self.logical_completed_requests is not None
            and self.logical_failed_requests is not None
            and self.logical_failed_requests > self.logical_completed_requests
        ):
            raise ValueError("Logical failures exceed completions")
        return self

    # Mypy cannot model Pydantic's computed-property decorator.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def ttft_seconds_mean(self) -> float | None:
        return (
            self.ttft_seconds_sum / self.ttft_known_samples
            if self.ttft_known_samples and self.ttft_seconds_sum is not None
            else None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def latency_seconds_mean(self) -> float | None:
        return (
            self.latency_seconds_sum / self.latency_known_samples
            if self.latency_known_samples and self.latency_seconds_sum is not None
            else None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def generation_tokens_per_second(self) -> float | None:
        return (
            self.generation_output_tokens / self.generation_seconds_sum
            if self.generation_seconds_sum and self.generation_output_tokens is not None
            else None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def end_to_end_tokens_per_second(self) -> float | None:
        return (
            self.end_to_end_output_tokens / self.end_to_end_seconds_sum
            if self.end_to_end_seconds_sum and self.end_to_end_output_tokens is not None
            else None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attempt_failure_rate(self) -> float | None:
        return (
            self.failed_dispatched_attempts / self.dispatched_attempts
            if self.dispatched_attempts and self.failed_dispatched_attempts is not None
            else None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def logical_failure_rate(self) -> float | None:
        return (
            self.logical_failed_requests / self.logical_completed_requests
            if self.logical_completed_requests and self.logical_failed_requests is not None
            else None
        )


class UsageCacheMetrics(UsageModel):
    """Provider prompt-cache statistics, not gateway response-cache statistics."""

    eligible_requests: Count | None = None
    hit_requests: Count | None = None
    eligible_input_tokens: Count | None = None
    cache_read_tokens: Count | None = None


class UsageStates(UsageModel):
    """Finalized state counts plus explicitly requested, bounded pending counts."""

    observed_start: Count | None = None
    inflight: Count | None = None
    succeeded: Count = 0
    failed: Count = 0
    cancelled: Count = 0
    unknown: Count = 0


class UsageMetrics(UsageModel):
    """Projected terminal contributions and optional captured-identity counts.

    Observed attempts include final-only captures whose start was lost. These
    counts cannot establish how many real attempts escaped asynchronous capture.
    """

    finalized_attempts: Count = 0
    unpriced_attempts: Count | None = None
    partially_priced_attempts: Count | None = None
    unallocatable_cost_attempts: Count | None = None
    observed_attempts: Count | None = None
    pending_complete: bool | None = None
    states: UsageStates = Field(default_factory=UsageStates)
    tokens: TokenTotals = Field(default_factory=TokenTotals)
    token_known_attempts: TokenCoverage = Field(default_factory=TokenCoverage)
    costs: list[CostSubtotal] = Field(default_factory=list)
    reported_attempts: Count = 0
    partial_attempts: Count = 0
    unknown_usage_attempts: Count = 0
    performance: UsagePerformance = Field(default_factory=UsagePerformance)
    logical_performance: UsageLogicalPerformance = Field(default_factory=UsageLogicalPerformance)
    prompt_cache: UsageCacheMetrics = Field(default_factory=UsageCacheMetrics)


class UsageMeta(UsageModel):
    """Applied scope, projection coverage, and explicit exclusions."""

    scope: UsageScope
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    precision: Literal["hour", "day", "exact", "lifetime"]
    coverage_start: AwareDatetime | None = None
    observed_through: AwareDatetime | None = None
    complete: bool
    capture: UsageCaptureCoverage = Field(default_factory=UsageCaptureCoverage)
    projection: UsageProjectionCoverage = Field(default_factory=UsageProjectionCoverage)
    availability: UsageAvailability = Field(default_factory=UsageAvailability)
    accounting_degraded: bool = False
    private_resources_excluded: bool = False
    excluded_fields: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_observed_source(self) -> Self:
        if self.complete and (
            self.capture.state != "observed"
            or self.projection.state != "ready"
            or self.availability.resolution in {"unavailable", "detail_expired"}
        ):
            raise ValueError(
                "Complete observed results need known capture, projection and fidelity"
            )
        return self


class UsageGroup(UsageModel):
    """One authorized ranking/facet value."""

    key: str | None
    label: str | None
    metrics: UsageMetrics


class UsageCostBreakdown(UsageModel):
    """Complete marginal population; monetary bands stay separate inside metrics."""

    dimension: Literal["subject", "agent", "provider", "model"]
    groups: list[UsageGroup]


class UsageCostBreakdowns(UsageModel):
    """Known components, including partial valuations, not complete spend."""

    measure: Literal["known_priced_subtotal"] = "known_priced_subtotal"
    complete_groups: Literal[True] = True
    dimensions: list[UsageCostBreakdown]


class UsageBucket(UsageModel):
    """Observed-source completeness, not a claim of crash-proof capture."""

    start: AwareDatetime
    end: AwareDatetime
    complete: bool
    metrics: UsageMetrics
    groups: list[UsageGroup] = Field(default_factory=list)
    capture: UsageCaptureCoverage = Field(default_factory=UsageCaptureCoverage)
    projection: UsageProjectionCoverage = Field(default_factory=UsageProjectionCoverage)
    source_grain: Literal["hour", "day"] | None = None
    source: Literal["retained", "archive", "mixed"] = "retained"


class TokenPrediction(UsageModel):
    """Independent token-field prediction, rounded half-up to whole tokens."""

    available: bool
    projected_tokens: Count | None = None
    reason: ForecastReason | None = None


class CostPrediction(UsageModel):
    """Cost availability is independent of token availability."""

    available: bool
    projected_cost: CostSubtotal | None = None
    reason: ForecastReason | None = None


class UsageForecast(UsageModel):
    """Constant-rate arithmetic, not a confidence estimate."""

    method: Literal["constant_rate_v1"] = "constant_rate_v1"
    sample_start: AwareDatetime | None
    sample_end: AwareDatetime | None
    horizon_seconds: int
    tokens: dict[
        Literal["input", "output", "cache_read", "cache_write", "reasoning"], TokenPrediction
    ]
    cost: CostPrediction
    limitations: list[str]


class UsageQueryResponse(UsageModel):
    """Shared REST/tool analytics response."""

    meta: UsageMeta
    total: UsageMetrics | None
    groups: list[UsageGroup] = Field(default_factory=list)
    buckets: list[UsageBucket] = Field(default_factory=list)
    forecast: UsageForecast | None = None
    cost_breakdowns: UsageCostBreakdowns | None = None
    ranking_measure: Literal["known_priced_subtotal", "attempts", "input", "output"] | None = None


class UsageSessionQuery(UsageAttemptQuery):
    """Paged cumulative Cognis or retained-detail proxy namespaces."""

    session_type: Literal["cognis", "proxy"] = "cognis"
    search: Annotated[str, Field(min_length=1, max_length=160)] | None = None


class UsageSessionItem(UsageModel):
    """A currently authorized session, not a fabricated companion service session."""

    session_type: Literal["cognis", "proxy"] = "cognis"
    session_id: Identifier | None = None
    session_label: str | None = None
    selection_ref: str | None = None
    totals_scope: Literal["cumulative", "retained_detail"] = "cumulative"
    client_id: Identifier | None = None
    client_session_id: Identifier | None = None
    retained_start: AwareDatetime | None = None
    retained_end: AwareDatetime | None = None
    conversation_id: Identifier | None = None
    metrics: UsageMetrics
    drill_filters: UsageFilters


class UsageSessionPage(UsageModel):
    """Bounded session page; the cursor retains the complete query identity."""

    meta: UsageMeta
    items: list[UsageSessionItem]
    next_cursor: str | None = None


class UsageRequestItem(UsageModel):
    """One captured logical request; missing statistics are not inferred from attempts."""

    request_id: Identifier | None = None
    attempt_id: Identifier | None = None
    selection_ref: str | None = None
    session_type: Literal["cognis", "proxy"] | None = None
    session_label: str | None = None
    cycle_id: Identifier | None = None
    state: AttemptState
    logical_request_started_at: AwareDatetime | None = None
    started_at: AwareDatetime
    logical_outcome: AttemptState | None = None
    timing: UsageLogicalTiming | None = None
    drill_filters: UsageFilters


class UsageRequestLog(UsageRequestItem):
    """Reduced logical request without private client/header provenance."""

    traffic_origin: Literal["cognis", "api", "unknown"]
    operations: list[str] = Field(default_factory=list)
    media_usage: list[UsageNumericExtra] = Field(default_factory=list)
    auth_type: str | None = None
    completed_at: AwareDatetime | None = None
    requested_model: UsageModelName | None = None
    provider_ids: list[Identifier] = Field(default_factory=list)
    resolved_models: list[UsageModelName] = Field(default_factory=list)
    request_path: str | None = None
    streaming: bool | None = None
    http_status_code: int | None = None
    finish_reason: str | None = None
    service_id: Identifier | None = None
    error_category: ErrorCategory | None = None
    error_summary: str | None = None
    latency_seconds: Seconds | None = None
    ttft_seconds: Seconds | None = None
    tokens: TokenTotals = Field(default_factory=TokenTotals)
    usage_quality: UsageQuality = "unknown"
    costs: list[CostSubtotal] = Field(default_factory=list)
    unpriced_attempts: Count = 0
    attempt_count: Count = 0
    retry_count: Count = 0
    fallback_count: Count = 0


class UsagePrivateRequestLog(UsageRequestLog):
    """Private metadata is only constructed for personal/admin authorization."""

    client_id: Identifier | None = None
    key_id: Identifier | None = None
    user_agent: str | None = None
    app_name: str | None = None
    app_version: str | None = None
    client_labels: UsageClientLabels | None = None


class UsageRequestPage(UsageModel):
    """Unique request rows, filtered through currently authorized indexed attempts."""

    meta: UsageMeta
    pagination_unit: Literal["attempt", "request"] = "attempt"
    items: list[UsagePrivateRequestLog | UsageRequestLog | UsageRequestItem]
    next_cursor: str | None = None

    @model_validator(mode="after")
    def private_scope(self) -> Self:
        if self.meta.scope.mode == "project" and any(
            isinstance(item, UsagePrivateRequestLog) for item in self.items
        ):
            raise ValueError("Project requests must use the reduced schema")
        return self


class UsageSummarySelector(UsageModel):
    """One visible sidebar row; resolving it does not imply authorization."""

    kind: Literal["conversation", "activity", "session", "execution"]
    id: Identifier


class UsageSummaryRequest(UsageModel):
    """One bounded sidebar page request."""

    scope: UsageScope = Field(default_factory=UsageScope)
    items: Annotated[list[UsageSummarySelector], Field(min_length=1, max_length=100)]
    inclusion: Literal["self", "direct", "inclusive"] = "self"
    pricing_basis: Literal["historical", "current"] = "historical"
    include_pending: bool = False

    @model_validator(mode="after")
    def supported_population(self) -> Self:
        if self.inclusion == "direct" and any(item.kind == "activity" for item in self.items):
            raise ValueError("Direct activity accounting has no canonical execution anchor")
        if self.inclusion == "self" and any(item.kind == "execution" for item in self.items):
            raise ValueError("Execution selectors require direct or inclusive")
        return self


class UsageSummaryItem(UsageModel):
    """Unavailable includes unknown and unauthorized identifiers alike."""

    selector: UsageSummarySelector
    available: bool
    inclusion: Literal["self", "direct", "inclusive"]
    physical_sessions: Count | None = None
    metrics: UsageMetrics | None = None
    availability: UsageAvailability = Field(default_factory=UsageAvailability)

    @model_validator(mode="after")
    def unavailable_is_not_existence_evidence(self) -> Self:
        if not self.available and (
            self.metrics is not None
            or self.physical_sessions is not None
            or self.availability.resolution != "unavailable"
        ):
            raise ValueError("Unavailable selectors cannot expose accounting or retention evidence")
        if self.available and (
            self.metrics is None or self.availability.resolution == "unavailable"
        ):
            raise ValueError("Available selectors require explicit accounting fidelity")
        return self


class UsageSummaryResponse(UsageModel):
    """Batched sidebar totals, never additive across overlapping selectors."""

    meta: UsageMeta
    items: list[UsageSummaryItem]


class UsageAttemptDetail(UsageModel):
    """Reduced content-free detail safe for an authorized project-team row."""

    attempt_id: str | None = None
    media_usage: list[UsageNumericExtra] = Field(default_factory=list)
    traffic_origin: Literal["cognis", "api", "unknown"] = "unknown"
    inference_operation: UsageOperation | None = None
    auth_type: str | None = None
    state: AttemptState
    subject_email: str | None
    subject_kind: Literal["user", "service", "unattributed"] = "user"
    project_id: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    step_run_id: str | None = None
    turn_id: str | None = None
    cycle_id: str | None = None
    logical_request_id: str | None = None
    selection_ref: str | None = None
    agent_id: str | None = None
    provider_id: str | None = None
    requested_model: UsageModelName | None = None
    resolved_model: UsageModelName | None = None
    reported_model: UsageModelName | None = None
    effort: str | None = None
    purpose: str | None = None
    hook_id: str | None = None
    hook_name: UsageDisplayLabel | None = None
    hook_type: str | None = None
    hook_invocation_id: str | None = None
    service_id: str | None = None
    operation: str | None = None
    started_at: AwareDatetime
    dispatched_at: AwareDatetime | None = None
    first_generation_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    ttft_seconds: Seconds | None = None
    latency_seconds: Seconds | None = None
    generation_seconds: Seconds | None = None
    generation_tokens_per_second: Seconds | None = None
    end_to_end_tokens_per_second: Seconds | None = None
    provider_timing_source: Literal[
        "controller_monotonic", "executor_monotonic", "provider_reported", "unknown"
    ] = "unknown"
    logical_timing: UsageLogicalTiming | None = None
    parameters: UsageRequestParameters = Field(default_factory=UsageRequestParameters)
    retry: bool = False
    fallback: bool = False
    streaming: bool | None = None
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None
    error_category: ErrorCategory | None = None
    exception_class: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")] | None = None
    provider_error_code: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")] | None = None
    tokens: TokenTotals
    usage_quality: UsageQuality
    costs: list[CostSubtotal]

    @model_validator(mode="after")
    def nonstream_ttft_requires_provider_measurement(self) -> Self:
        if (
            self.streaming is False
            and self.ttft_seconds is not None
            and self.provider_timing_source != "provider_reported"
        ):
            raise ValueError("Nonstream TTFT is unavailable without provider measurement")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def error_summary(self) -> str | None:
        """Fixed templates cannot accidentally echo provider exception payloads."""
        summaries = {
            "authentication": "Provider authentication failed.",
            "rate_limit": "Provider rate limit reached.",
            "timeout": "Provider request timed out.",
            "connection": "Provider connection failed.",
            "cancelled": "Request was cancelled.",
            "invalid_request": "Provider rejected the request.",
            "provider": "Provider reported an error.",
            "internal": "Internal inference error.",
            "unknown": "Request outcome is unknown.",
        }
        return summaries.get(self.error_category) if self.error_category else None


class UsagePrivateAttemptDetail(UsageAttemptDetail):
    """Own-personal/admin provenance; never serialize this variant for team mode."""

    server_request_id: str | None = None
    provider_request_id: str | None = None
    key_id: str | None = None
    client_id: str | None = None
    client_request_id: str | None = None
    subject_id: str | None = None


class UsageAttemptPage(UsageModel):
    """Bounded keyset page, with an explicit safe detail variant."""

    meta: UsageMeta
    items: list[UsagePrivateAttemptDetail | UsageAttemptDetail]
    next_cursor: str | None = None

    @model_validator(mode="after")
    def reduced_project_schema(self) -> Self:
        if self.meta.scope.mode == "project" and any(
            isinstance(item, UsagePrivateAttemptDetail) for item in self.items
        ):
            raise ValueError("Project detail must use the reduced provenance schema")
        if self.meta.availability.resolution in {"detail_expired", "aggregate_only"} and (
            self.items or self.next_cursor is not None
        ):
            raise ValueError("Expired detail cannot return attempts or a detail continuation")
        return self


class UsageFacetRequest(UsageModel):
    """One authorized summary-backed facet; never a credential inventory."""

    scope: UsageScope = Field(default_factory=UsageScope)
    start: AwareDatetime
    end: AwareDatetime
    filters: UsageFilters = Field(default_factory=UsageFilters)
    dimension: UsageDimension
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 100

    @model_validator(mode="after")
    def valid_catalog_window(self) -> Self:
        UsageQuery(start=self.start, end=self.end, scope=self.scope, filters=self.filters)
        return self


class UsageFacetResponse(UsageModel):
    """Facet discovery uses the same current authorization as analytics."""

    meta: UsageMeta
    dimension: UsageDimension
    items: list[UsageGroup]


class UsageCatalogRequest(UsageModel):
    """Fresh capability discovery uses the same scope gate as data."""

    scope: UsageScope = Field(default_factory=UsageScope)


class UsageSelectionContract(UsageModel):
    """Public opaque-ref capabilities; private payloads are not a client contract."""

    token_format: Literal["aead_v2"] = "aead_v2"
    expires_after_seconds: Literal[3600] = 3600
    session_kinds: tuple[Literal["cognis", "proxy"], ...] = ("cognis", "proxy")
    proxy_totals: Literal["retained_detail"] = "retained_detail"
    proxy_search: Literal["exact_label_private_scope"] = "exact_label_private_scope"
    max_proxy_page: Literal[100] = 100
    detail_query: Literal["UsageAttemptQuery.selection_ref"] = "UsageAttemptQuery.selection_ref"
    team_identifiers: Literal["encrypted_refs_only"] = "encrypted_refs_only"


class UsageCatalog(UsageModel):
    """Supported closed query shapes, not an inventory or SQL interface."""

    scope: UsageScope
    grouping_dimensions: list[UsageDimension]
    summary_filters: list[str]
    detail_filters: list[str]
    summary_population_available: bool
    unavailable_metrics: dict[str, str]
    session_types: dict[str, str]
    selections: UsageSelectionContract = Field(default_factory=UsageSelectionContract)


class ProviderQuotaHistoryQuery(UsageModel):
    """Read stored samples only; this request cannot trigger provider refresh."""

    scope: UsageScope = Field(default_factory=UsageScope)
    start: AwareDatetime
    end: AwareDatetime
    window: Identifier | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100
    cursor: Annotated[str, Field(max_length=4096)] | None = None

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if not timedelta(0) < self.end - self.start <= timedelta(days=90):
            raise ValueError("Quota history requires a positive window of at most 90 days")
        return self


class ProviderQuotaSample(UsageModel):
    """Observed account sample; utilization is not a billed-token estimate."""

    sample_id: str
    observed_at: AwareDatetime
    account_generation: str
    window: str
    source: str
    reset_at: AwareDatetime | None = None
    limit: DecimalString | None = None
    remaining: DecimalString | None = None
    utilization: DecimalString | None = None
    maximum_utilization: DecimalString | None = None
    first_observed_at: AwareDatetime | None = None
    sample_count: Count


class ProviderQuotaHistory(UsageModel):
    """Owner/admin-only history; gaps are preserved rather than interpolated."""

    provider_id: str
    samples: list[ProviderQuotaSample]
    next_cursor: str | None = None
    limitations: list[str]


class UsageRequestDetail(UsageModel):
    """One logical header and one bounded page of its authorized physical attempts."""

    meta: UsageMeta
    request: UsagePrivateRequestLog | UsageRequestLog | None = None
    attempts: list[UsagePrivateAttemptDetail | UsageAttemptDetail] = Field(default_factory=list)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def private_scope(self) -> Self:
        if self.meta.scope.mode == "project" and (
            isinstance(self.request, UsagePrivateRequestLog)
            or any(isinstance(item, UsagePrivateAttemptDetail) for item in self.attempts)
        ):
            raise ValueError("Project detail must use the reduced schema")
        return self


class UsageToolQuery(UsageModel):
    """Analytics operation shares the exact REST input model."""

    operation: Literal["query"]
    query: UsageQuery


class UsageToolAttempt(UsageModel):
    """Retained physical-attempt/request explorer."""

    operation: Literal["attempts", "requests"]
    query: UsageRequestQuery

    @model_validator(mode="after")
    def physical_attempt_mode(self) -> Self:
        if self.operation == "attempts" and self.query.pagination_unit != "attempt":
            raise ValueError("Unique-request paging is only supported by requests")
        return self


class UsageToolRequestDetail(UsageModel):
    """Logical detail requires an encrypted, actor-bound request selection."""

    operation: Literal["request_detail"]
    query: UsageRequestDetailQuery


class UsageToolSession(UsageModel):
    """Cognis resource session explorer."""

    operation: Literal["sessions"]
    query: UsageSessionQuery


class UsageToolSummary(UsageModel):
    """One bounded sidebar page."""

    operation: Literal["session_summaries"]
    query: UsageSummaryRequest


class UsageToolFacet(UsageModel):
    """Current authorized facets."""

    operation: Literal["facets"]
    query: UsageFacetRequest


class UsageToolQuota(UsageModel):
    """Provider owner quota history."""

    operation: Literal["provider_history"]
    provider_id: Identifier
    query: ProviderQuotaHistoryQuery


class UsageToolArguments(UsageModel):
    """Single canonical controller schema, derived from the HTTP input models."""

    request: Annotated[
        UsageToolQuery
        | UsageToolAttempt
        | UsageToolRequestDetail
        | UsageToolSession
        | UsageToolSummary
        | UsageToolFacet
        | UsageToolQuota,
        Field(discriminator="operation"),
    ]
