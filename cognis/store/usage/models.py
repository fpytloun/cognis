"""Content-free facts/prices and reconstructable accounting projections.

Capture commits only canonical observations and an outbox intent. A separate
projector owns derived generations. Historical references never cascade with live
resources. Archive tables are reserved metadata; deletion is not enabled here.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cognis.store.models import Base

MONEY = Numeric(30, 12)
ROW_KEY = BigInteger().with_variant(Integer, "sqlite")
PENDING_PREDICATE = text("finalized_at IS NULL")
RETIRABLE_JOB_HISTORY = text("completed_at IS NOT NULL AND status IN ('complete', 'superseded')")


class UsageCounts:
    """Typed nullable facts; unknown values and zero have different meanings."""

    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    provider_total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_5m_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_1h_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger)
    input_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unknown")
    output_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unknown")
    cache_read_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown"
    )
    cache_write_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown"
    )
    reasoning_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown"
    )


class UsageTiming:
    """Bounded scalar timings; null means unavailable, including nonstream TTFT."""

    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    total_elapsed_ms: Mapped[float | None] = mapped_column(Numeric(24, 6, asdecimal=False))
    dispatch_elapsed_ms: Mapped[float | None] = mapped_column(Numeric(24, 6, asdecimal=False))
    provider_elapsed_ms: Mapped[float | None] = mapped_column(Numeric(24, 6, asdecimal=False))
    ttft_ms: Mapped[float | None] = mapped_column(Numeric(24, 6, asdecimal=False))
    first_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    generation_elapsed_ms: Mapped[float | None] = mapped_column(Numeric(24, 6, asdecimal=False))


class UsageTrafficDimensions:
    """Trusted sparse traffic dimensions, distinct from proxy-session correlation."""

    traffic_origin: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="unknown"
    )
    traffic_auth_type: Mapped[str | None] = mapped_column(String(20))
    traffic_client_id: Mapped[str | None] = mapped_column(String(160))
    traffic_key_id: Mapped[str | None] = mapped_column(String(160))


class UsageAttemptRow(UsageTrafficDimensions, UsageCounts, UsageTiming, Base):
    """Best-effort canonical attempt; terminal observations may arrive without start."""

    __tablename__ = "usage_attempts"

    row_id: Mapped[int] = mapped_column(ROW_KEY, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(String(160), nullable=False)
    request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    cycle_id: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt_ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retry_of_attempt_id: Mapped[str | None] = mapped_column(String(160))
    fallback_from_attempt_id: Mapped[str | None] = mapped_column(String(160))
    parent_operation_id: Mapped[str | None] = mapped_column(String(160))
    controller_owner_id: Mapped[str] = mapped_column(String(160), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(160))
    owner_key: Mapped[str] = mapped_column(String(330), nullable=False)
    usage_subject_key: Mapped[str] = mapped_column(String(330), nullable=False)
    principal_subject: Mapped[str | None] = mapped_column(String(320))
    principal_type: Mapped[str] = mapped_column(String(20), nullable=False)
    usage_subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    usage_subject_id: Mapped[str | None] = mapped_column(String(320))
    resource_owner: Mapped[str | None] = mapped_column(String(320))
    provider_payer: Mapped[str | None] = mapped_column(String(320))
    routing_owner: Mapped[str | None] = mapped_column(String(320))
    on_behalf_user: Mapped[str | None] = mapped_column(String(320))
    initiating_actor: Mapped[str | None] = mapped_column(String(320))
    auth_type: Mapped[str | None] = mapped_column(String(128))
    service_id: Mapped[str | None] = mapped_column(String(160))
    service_operation: Mapped[str | None] = mapped_column(String(128))
    client_id: Mapped[str | None] = mapped_column(String(160))
    client_session_id: Mapped[str | None] = mapped_column(String(160))
    key_record_id: Mapped[str | None] = mapped_column(String(160))
    token_record_id: Mapped[str | None] = mapped_column(String(160))
    project_id: Mapped[str | None] = mapped_column(String(160))
    conversation_id: Mapped[str | None] = mapped_column(String(160))
    session_id: Mapped[str | None] = mapped_column(String(160))
    activity_scope_id: Mapped[str | None] = mapped_column(String(160))
    task_id: Mapped[str | None] = mapped_column(String(160))
    step_run_id: Mapped[str | None] = mapped_column(String(160))
    turn_id: Mapped[str | None] = mapped_column(String(160))
    agent_id: Mapped[str | None] = mapped_column(String(160))
    agent_profile_id: Mapped[str | None] = mapped_column(String(160))
    binding_provenance: Mapped[str | None] = mapped_column(String(32))
    hook_id: Mapped[str | None] = mapped_column(String(160))
    hook_revision: Mapped[int | None] = mapped_column(BigInteger)
    hook_name: Mapped[str | None] = mapped_column(String(128))
    hook_type: Mapped[str | None] = mapped_column(String(128))
    hook_invocation_id: Mapped[str | None] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(160), nullable=False)
    effective_model: Mapped[str] = mapped_column(String(512), nullable=False)
    requested_model: Mapped[str | None] = mapped_column(String(512))
    requested_route: Mapped[str | None] = mapped_column(String(160))
    requested_effort: Mapped[str | None] = mapped_column(String(128))
    effective_effort: Mapped[str | None] = mapped_column(String(128))
    reasoning_budget: Mapped[int | None] = mapped_column(BigInteger)
    service_tier: Mapped[str | None] = mapped_column(String(128))
    cache_ttl: Mapped[str | None] = mapped_column(String(8))
    streaming: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    captured_price_revision_id: Mapped[str | None] = mapped_column(String(160))
    resolver_snapshot_id: Mapped[str | None] = mapped_column(String(160))
    requested_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    effective_parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    executor_id: Mapped[str | None] = mapped_column(String(160))
    executor_request_id: Mapped[str | None] = mapped_column(String(160))
    coverage_epoch_id: Mapped[str] = mapped_column(String(160), nullable=False)
    started_at_observed: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    bucket_day: Mapped[date] = mapped_column(Date, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    fact_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    context_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    start_observation_id: Mapped[str | None] = mapped_column(String(160))
    start_digest: Mapped[str | None] = mapped_column(String(64))
    start_observed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    projected_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    terminal_digest: Mapped[str | None] = mapped_column(String(64))
    terminal_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observation_id: Mapped[str | None] = mapped_column(String(160))
    finalized_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(20))
    dispatch_status: Mapped[str | None] = mapped_column(String(32))
    provider_request_id: Mapped[str | None] = mapped_column(String(160))
    provider_reported_model: Mapped[str | None] = mapped_column(String(512))
    usage_completeness: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="unknown"
    )
    normalization_version: Mapped[int | None] = mapped_column(Integer)
    semantic_profile: Mapped[str | None] = mapped_column(String(128))
    semantic_version: Mapped[int | None] = mapped_column(Integer)
    cache_read_semantics: Mapped[str | None] = mapped_column(String(32))
    cache_write_semantics: Mapped[str | None] = mapped_column(String(32))
    reasoning_semantics: Mapped[str | None] = mapped_column(String(32))
    cache_eligible: Mapped[bool | None] = mapped_column(Boolean)
    gateway_response_cache_hit: Mapped[bool | None] = mapped_column(Boolean)
    performance_source: Mapped[str | None] = mapped_column(String(20))
    error_category: Mapped[str | None] = mapped_column(String(32))
    error_summary_code: Mapped[str | None] = mapped_column(String(48))
    exception_class: Mapped[str | None] = mapped_column(String(96))
    provider_error_code: Mapped[str | None] = mapped_column(String(64))
    http_status: Mapped[int | None] = mapped_column(Integer)
    execution_ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    execution_end_reason: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_usage_attempt_id"),
        CheckConstraint("fact_version >= 0", name="ck_usage_attempt_version"),
        CheckConstraint("attempt_ordinal >= 0", name="ck_usage_attempt_ordinal"),
        CheckConstraint(
            "(fact_version = 0 AND terminal_digest IS NULL AND finalized_at IS NULL) OR "
            "(fact_version > 0 AND terminal_digest IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_usage_attempt_terminal",
        ),
        CheckConstraint(
            "projected_version >= 0 AND projected_version <= fact_version",
            name="ck_usage_projected_version",
        ),
        Index("ix_usage_subject_time", "usage_subject_key", "started_at_observed", "row_id"),
        Index(
            "ix_usage_owner_session_time",
            "owner_key",
            "session_id",
            "started_at_observed",
            "row_id",
        ),
        Index(
            "ix_usage_provider_model_time",
            "provider_id",
            "effective_model",
            "started_at_observed",
            "row_id",
        ),
        Index("ix_usage_request_attempt", "request_id", "attempt_ordinal"),
        Index("ix_usage_attempt_time", "started_at_observed", "row_id"),
        Index(
            "ix_usage_attempt_scope_expiry",
            "owner_key",
            "bucket_day",
            "finalized_at",
            "row_id",
            sqlite_where=text("fact_version > 0 AND projected_version = fact_version"),
            postgresql_where=text("fact_version > 0 AND projected_version = fact_version"),
        ),
        Index(
            "ix_usage_client_session_time",
            "usage_subject_key",
            "client_id",
            "client_session_id",
            "started_at_observed",
            "row_id",
            sqlite_where=text("client_session_id IS NOT NULL"),
            postgresql_where=text("client_session_id IS NOT NULL"),
        ),
        Index("ix_usage_owner_day_row", "owner_key", "bucket_day", "row_id"),
        Index(
            "ix_usage_next_expiry",
            "finalized_at",
            "row_id",
            sqlite_where=text("finalized_at IS NOT NULL AND projected_version = fact_version"),
            postgresql_where=text("finalized_at IS NOT NULL AND projected_version = fact_version"),
        ),
        Index(
            "ix_usage_pending_owner",
            "controller_owner_id",
            "owner_key",
            "started_at_observed",
            "row_id",
            sqlite_where=PENDING_PREDICATE,
            postgresql_where=PENDING_PREDICATE,
        ),
    )


class UsageCorrectionRow(Base):
    """Immutable correction audit; facts contain the latest accepted version."""

    __tablename__ = "usage_corrections"
    correction_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(160), nullable=False)
    prior_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_terminal_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    replacement_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("attempt_id", "new_version", name="uq_usage_correction_version"),
        CheckConstraint("new_version = prior_version + 1", name="ck_usage_correction_step"),
    )


class UsagePriceRevisionRow(Base):
    """Canonical immutable definition, published once per metadata change."""

    __tablename__ = "usage_price_revisions"
    revision_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(160), nullable=False)
    model: Mapped[str] = mapped_column(String(512), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    basis: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(160))
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("ix_usage_price_effective", "provider_id", "model", "effective_at"),)


class UsageHistoricalPriceBindingRow(Base):
    """Canonical explicit backfill authority; valuations remain disposable."""

    __tablename__ = "usage_historical_price_bindings"
    attempt_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(160), nullable=False)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class UsagePricingStateRow(Base):
    """Shared projection/exclusive publication fence, never taken by raw capture."""

    __tablename__ = "usage_pricing_state"
    provider_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    model: Mapped[str] = mapped_column(String(512), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("generation >= 0", name="ck_usage_pricing_generation"),)


class UsageProjectionStateRow(Base):
    """Active/build owner/day generations fence all finalized mutation paths."""

    __tablename__ = "usage_projection_state"
    owner_key: Mapped[str] = mapped_column(String(330), primary_key=True)
    bucket_day: Mapped[date] = mapped_column(Date, primary_key=True)
    active_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    building_generation: Mapped[int | None] = mapped_column(BigInteger)
    source_cut_row_id: Mapped[int | None] = mapped_column(BigInteger)
    source_cut_outbox_id: Mapped[int | None] = mapped_column(BigInteger)
    build_job_id: Mapped[str | None] = mapped_column(String(160))
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    traffic_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    capture_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exclusive_job_kind: Mapped[str | None] = mapped_column(String(20))
    exclusive_job_id: Mapped[str | None] = mapped_column(String(160))
    detail_expired_before: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    oldest_detail_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    retention_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    archive_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("active_generation >= 1", name="ck_usage_projection_generation"),
        Index("ix_usage_projection_day", "bucket_day", "owner_key"),
        Index(
            "ix_usage_scope_expiry",
            "oldest_detail_at",
            "owner_key",
            "bucket_day",
            sqlite_where=text(
                "oldest_detail_at IS NOT NULL AND retention_paused = false AND ready = true AND exclusive_job_id IS NULL"
            ),
            postgresql_where=text(
                "oldest_detail_at IS NOT NULL AND retention_paused = false AND ready = true AND exclusive_job_id IS NULL"
            ),
        ),
        CheckConstraint(
            "(building_generation IS NULL AND source_cut_row_id IS NULL AND build_job_id IS NULL) OR "
            "(building_generation > active_generation AND source_cut_row_id IS NOT NULL "
            "AND build_job_id IS NOT NULL)",
            name="ck_usage_projection_build",
        ),
    )


class UsageValuationRow(Base):
    """Conditional contribution marker and disposable reproducible valuation."""

    __tablename__ = "usage_valuations"
    owner_key: Mapped[str] = mapped_column(String(330), primary_key=True)
    bucket_day: Mapped[date] = mapped_column(Date, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    fact_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    calculator_version: Mapped[int] = mapped_column(Integer, nullable=False)
    pricing_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    historical_revision_id: Mapped[str | None] = mapped_column(String(160))
    current_revision_id: Mapped[str | None] = mapped_column(String(160))
    historical_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    current_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    historical_status: Mapped[str] = mapped_column(String(20), nullable=False)
    current_status: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    basis: Mapped[str | None] = mapped_column(String(32))
    contribution_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    contribution: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("fact_version >= 1", name="ck_usage_valuation_version"),
        CheckConstraint(
            "source_kind IN ('attempt', 'request', 'archive')", name="ck_usage_valuation_source"
        ),
    )


class UsageSummaryMeasures:
    """Additive sums/counts only; no pending rows or timing arrays."""

    finalized_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    success_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    error_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cancelled_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    not_dispatched_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unknown_outcome_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_observed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_accepted_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reported_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partial_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unknown_usage_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_known_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_known_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_eligible_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_eligibility_known_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_known_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_positive_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_unknown_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_write_known_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_write_positive_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_write_unknown_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    historical_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    current_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    historical_priced_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_priced_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    historical_partial_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_partial_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_elapsed_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_elapsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_elapsed_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    dispatch_elapsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_elapsed_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    provider_elapsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ttft_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    ttft_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation_elapsed_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    generation_elapsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation_output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation_output_ms: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    generation_output_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logical_completed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logical_error_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_elapsed_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    server_elapsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    server_ttft_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    server_ttft_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_elapsed_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    client_elapsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_ttft_ms_sum: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    client_ttft_count: Mapped[int] = mapped_column(BigInteger, nullable=False)


class UsageSummaryDimensions(UsageTrafficDimensions):
    """Fixed sparse dimensions; never a key/client/request rollup cube."""

    owner_key: Mapped[str] = mapped_column(String(330), primary_key=True)
    bucket_day: Mapped[date] = mapped_column(Date, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dimension_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    usage_subject_key: Mapped[str] = mapped_column(String(330), nullable=False)
    resource_owner: Mapped[str | None] = mapped_column(String(320))
    project_id: Mapped[str | None] = mapped_column(String(160))
    agent_id: Mapped[str | None] = mapped_column(String(160))
    agent_profile_id: Mapped[str | None] = mapped_column(String(160))
    provider_id: Mapped[str | None] = mapped_column(String(160))
    effective_model: Mapped[str | None] = mapped_column(String(512))
    effective_effort: Mapped[str | None] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    service_id: Mapped[str | None] = mapped_column(String(160))
    service_operation: Mapped[str | None] = mapped_column(String(128))
    hook_id: Mapped[str | None] = mapped_column(String(160))
    hook_type: Mapped[str | None] = mapped_column(String(128))
    streaming: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(20))
    currency: Mapped[str | None] = mapped_column(String(3))
    basis: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class UsageResourceDimensions:
    """Resource-grain dimensions for current-authority joins."""

    session_id: Mapped[str | None] = mapped_column(String(160))
    conversation_id: Mapped[str | None] = mapped_column(String(160))
    activity_scope_id: Mapped[str | None] = mapped_column(String(160))
    task_id: Mapped[str | None] = mapped_column(String(160))
    step_run_id: Mapped[str | None] = mapped_column(String(160))


SESSION_CORRELATION_CHECK = (
    "(session_kind = 'cognis' AND session_id IS NOT NULL "
    "AND client_id IS NULL AND client_session_id IS NULL) OR "
    "(session_kind = 'proxy' AND session_id IS NULL AND client_session_id IS NOT NULL) OR "
    "(session_kind = 'direct' AND conversation_id IS NOT NULL AND session_id IS NULL "
    "AND client_id IS NULL AND client_session_id IS NULL)"
)


class UsageSessionTotalRow(
    UsageSummaryMeasures, UsageSummaryDimensions, UsageResourceDimensions, Base
):
    """Physical-session/day contributions, never ancestor counters."""

    __tablename__ = "usage_session_totals"
    session_kind: Mapped[str] = mapped_column(String(6), nullable=False, server_default="cognis")
    client_id: Mapped[str | None] = mapped_column(String(160))
    client_session_id: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        CheckConstraint(
            SESSION_CORRELATION_CHECK,
            name="ck_usage_session_correlation",
        ),
        Index("ix_usage_session_subject", "usage_subject_key", "session_id", "bucket_day"),
        Index("ix_usage_session_day", "bucket_day", "owner_key", "generation"),
        Index(
            "ix_usage_direct_conversation",
            "conversation_id",
            "bucket_day",
            "owner_key",
            "generation",
            sqlite_where=text("session_kind = 'direct'"),
            postgresql_where=text("session_kind = 'direct'"),
        ),
        Index(
            "ix_usage_proxy_session",
            "usage_subject_key",
            "client_id",
            "client_session_id",
            "bucket_day",
            sqlite_where=text("session_kind = 'proxy'"),
            postgresql_where=text("session_kind = 'proxy'"),
        ),
    )


class UsageHourlyTotalRow(
    UsageSummaryMeasures, UsageSummaryDimensions, UsageResourceDimensions, Base
):
    """Authorization-preserving hourly facts, including session-less services."""

    __tablename__ = "usage_hourly_totals"
    bucket_hour: Mapped[int] = mapped_column(Integer, primary_key=True)
    __table_args__ = (
        CheckConstraint("bucket_hour BETWEEN 0 AND 23", name="ck_usage_hour"),
        Index("ix_usage_hourly_subject_day", "usage_subject_key", "bucket_day"),
        Index("ix_usage_hourly_day", "bucket_day", "bucket_hour", "owner_key", "generation"),
        Index("ix_usage_hourly_project_session", "project_id", "session_id", "bucket_day"),
    )


class UsageDailyTotalRow(UsageSummaryMeasures, UsageSummaryDimensions, Base):
    """Coarse totals; insufficient for cross-user project resource permissions."""

    __tablename__ = "usage_daily_totals"
    __table_args__ = (
        Index("ix_usage_daily_subject_day", "usage_subject_key", "bucket_day"),
        Index("ix_usage_daily_day", "bucket_day", "owner_key", "generation"),
    )


class UsageMaintenanceJobRow(Base):
    """Explicit non-polling job intent with fenced owner and transactional cursor."""

    __tablename__ = "usage_maintenance_jobs"
    job_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    target_pricing_generation: Mapped[int | None] = mapped_column(BigInteger)
    owner_id: Mapped[str | None] = mapped_column(String(160))
    owner_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    processed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "kind IN ('backfill', 'reprice', 'rebuild', 'archive', 'cleanup')",
            name="ck_usage_job_kind",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'failed', 'complete', 'superseded')",
            name="ck_usage_job_status",
        ),
        Index("ix_usage_jobs_status_created", "status", "created_at", "job_id"),
        Index("ix_usage_jobs_kind_status", "kind", "status", "created_at", "job_id"),
        Index(
            "ix_usage_jobs_completed",
            "completed_at",
            "job_id",
            postgresql_where=RETIRABLE_JOB_HISTORY,
            sqlite_where=RETIRABLE_JOB_HISTORY,
        ),
    )


class ProviderUsageSampleRow(Base):
    """Canonical existing refresh/header observations, never extra polling."""

    __tablename__ = "provider_usage_samples"
    sample_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_owner: Mapped[str] = mapped_column(String(160), nullable=False)
    account_scope_id: Mapped[str] = mapped_column(String(160), nullable=False)
    configuration_generation: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    observation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    window_key: Mapped[str] = mapped_column(String(160), nullable=False)
    model_bucket: Mapped[str | None] = mapped_column(String(512))
    first_observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    reset_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    limit_value: Mapped[Decimal | None] = mapped_column(MONEY)
    latest_remaining: Mapped[Decimal | None] = mapped_column(MONEY)
    minimum_remaining: Mapped[Decimal | None] = mapped_column(MONEY)
    maximum_utilization: Mapped[Decimal | None] = mapped_column(MONEY)
    sample_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "account_scope_id",
            "configuration_generation",
            "source",
            "observation_id",
            "window_key",
            name="uq_usage_sample_observation",
        ),
        Index("ix_usage_sample_owner_time", "provider_owner", "provider_id", "last_observed_at"),
        Index("ix_usage_sample_expiry", "last_observed_at", "sample_id"),
    )


class UsageRequestStatsRow(Base):
    """Canonical separate server/client spans; not a second provider attempt."""

    __tablename__ = "usage_request_stats"
    request_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    cycle_id: Mapped[str | None] = mapped_column(String(160))
    attempt_id: Mapped[str | None] = mapped_column(String(160))
    request_started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(330), nullable=False)
    usage_subject_key: Mapped[str] = mapped_column(String(330), nullable=False)
    bucket_day: Mapped[date] = mapped_column(Date, nullable=False)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    controller_owner_id: Mapped[str] = mapped_column(String(160), nullable=False)
    coverage_epoch_id: Mapped[str] = mapped_column(String(160), nullable=False)
    server_observation_id: Mapped[str | None] = mapped_column(String(160))
    server_digest: Mapped[str | None] = mapped_column(String(64))
    server_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    client_observation_id: Mapped[str | None] = mapped_column(String(160))
    client_digest: Mapped[str | None] = mapped_column(String(64))
    client_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    logical_outcome: Mapped[str | None] = mapped_column(String(20))
    server_elapsed_ms: Mapped[Decimal | None] = mapped_column(MONEY)
    server_ttft_ms: Mapped[Decimal | None] = mapped_column(MONEY)
    client_elapsed_ms: Mapped[Decimal | None] = mapped_column(MONEY)
    client_ttft_ms: Mapped[Decimal | None] = mapped_column(MONEY)
    fact_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projected_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("fact_version >= 1", name="ck_usage_request_version"),
        Index("ix_usage_request_subject_time", "usage_subject_key", "request_started_at"),
        Index(
            "ix_usage_request_expiry",
            "owner_key",
            "bucket_day",
            "updated_at",
            "request_id",
            sqlite_where=text("logical_outcome IS NOT NULL AND projected_version = fact_version"),
            postgresql_where=text(
                "logical_outcome IS NOT NULL AND projected_version = fact_version"
            ),
        ),
    )


class UsageProjectionOutboxRow(Base):
    """Durable nonempty capture-batch intent; pending rows must never be pruned."""

    __tablename__ = "usage_projection_outbox"
    row_id: Mapped[int] = mapped_column(ROW_KEY, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    controller_owner_id: Mapped[str] = mapped_column(String(160), nullable=False)
    item_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    claimed_by: Mapped[str | None] = mapped_column(String(160))
    claim_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    __table_args__ = (
        CheckConstraint("cursor >= 0 AND cursor <= 64", name="ck_usage_outbox_cursor"),
        Index(
            "ix_usage_outbox_pending",
            "row_id",
            sqlite_where=text("completed_at IS NULL"),
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index("ix_usage_outbox_completed", "completed_at", "row_id"),
    )


class UsageCoverageEpochRow(Base):
    """Bounded persisted loss diagnostics; abrupt death implies unknown coverage."""

    __tablename__ = "usage_coverage_epochs"
    epoch_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    controller_owner_id: Mapped[str] = mapped_column(String(160), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dropped_started: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    dropped_finalized: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    dropped_stats: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    metadata_reduced_events: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    coverage_status: Mapped[str] = mapped_column(String(20), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "coverage_status IN ('open', 'complete', 'gapped', 'unknown')",
            name="ck_usage_coverage_status",
        ),
    )


class UsageArchiveManifestRow(Base):
    """Reserved archive checkpoint. No archive/delete implementation is enabled."""

    __tablename__ = "usage_archive_manifests"
    manifest_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(330), nullable=False)
    bucket_day: Mapped[date] = mapped_column(Date, nullable=False)
    archive_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail_cutoff: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    source_cut_row_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_cut_outbox_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "owner_key", "bucket_day", "archive_revision", name="uq_usage_archive_revision"
        ),
    )


class UsageArchivedTotalRow(
    UsageTrafficDimensions, UsageSummaryMeasures, UsageResourceDimensions, Base
):
    """Canonical compact evidence, never disposable after terminal detail expiry."""

    __tablename__ = "usage_archived_totals"
    archive_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    manifest_id: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(330), nullable=False)
    bucket_day: Mapped[date] = mapped_column(Date, nullable=False)
    bucket_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(64), nullable=False)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    usage_subject_key: Mapped[str] = mapped_column(String(330), nullable=False)
    resource_owner: Mapped[str | None] = mapped_column(String(320))
    project_id: Mapped[str | None] = mapped_column(String(160))
    agent_id: Mapped[str | None] = mapped_column(String(160))
    agent_profile_id: Mapped[str | None] = mapped_column(String(160))
    provider_id: Mapped[str | None] = mapped_column(String(160))
    model: Mapped[str | None] = mapped_column(String(512))
    effort: Mapped[str | None] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    hook_id: Mapped[str | None] = mapped_column(String(160))
    hook_revision: Mapped[int | None] = mapped_column(BigInteger)
    hook_type: Mapped[str | None] = mapped_column(String(128))
    service_id: Mapped[str | None] = mapped_column(String(160))
    service_operation: Mapped[str | None] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    record_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    historical_revision_id: Mapped[str | None] = mapped_column(String(160))
    current_revision_id: Mapped[str | None] = mapped_column(String(160))
    currency: Mapped[str | None] = mapped_column(String(3))
    basis: Mapped[str | None] = mapped_column(String(32))
    semantic_profile: Mapped[str | None] = mapped_column(String(128))
    semantic_version: Mapped[int | None] = mapped_column(Integer)
    billing_stratum_key: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_units: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    exact_reprice_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_detail_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "manifest_id",
            "dimension_key",
            "billing_stratum_key",
            name="uq_usage_archive_stratum",
        ),
        CheckConstraint("bucket_hour BETWEEN 0 AND 23", name="ck_usage_archive_hour"),
        Index("ix_usage_archive_subject_day", "usage_subject_key", "bucket_day"),
        Index("ix_usage_archive_resource_day", "project_id", "session_id", "bucket_day"),
    )


class UsageRequestLogRow(UsageTrafficDimensions, UsageResourceDimensions, Base):
    """Reconstructable private request headers, never a second billing ledger."""

    __tablename__ = "usage_request_logs"
    row_id: Mapped[int] = mapped_column(ROW_KEY, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_key: Mapped[str] = mapped_column(String(330), nullable=False)
    usage_subject_key: Mapped[str] = mapped_column(String(330), nullable=False)
    resource_owner: Mapped[str | None] = mapped_column(String(320))
    project_id: Mapped[str | None] = mapped_column(String(160))
    agent_id: Mapped[str | None] = mapped_column(String(160))
    agent_profile_id: Mapped[str | None] = mapped_column(String(160))
    bucket_day: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    http_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    logical_outcome: Mapped[str | None] = mapped_column(String(20))
    __table_args__ = (
        UniqueConstraint(
            "owner_key", "usage_subject_key", "request_id", name="uq_usage_request_log_identity"
        ),
        Index("ix_usage_request_log_request", "request_id"),
        Index("ix_usage_request_log_time", "started_at", "row_id"),
        Index("ix_usage_request_log_subject", "usage_subject_key", "started_at", "row_id"),
        Index("ix_usage_request_log_resource", "project_id", "session_id", "started_at"),
    )


for _model in (
    UsageAttemptRow,
    UsageSessionTotalRow,
    UsageHourlyTotalRow,
    UsageDailyTotalRow,
    UsageRequestLogRow,
):
    _at = (
        "started_at_observed"
        if _model is UsageAttemptRow
        else ("started_at" if _model is UsageRequestLogRow else "bucket_day")
    )
    for _dimension in ("origin", "auth_type", "client_id", "key_id"):
        Index(
            f"ix_{_model.__tablename__}_{_dimension}",
            _model.__table__.c.usage_subject_key,
            _model.__table__.c[f"traffic_{_dimension}"],
            _model.__table__.c[_at],
        )


USAGE_TABLE_NAMES = (
    "usage_attempts",
    "usage_corrections",
    "usage_price_revisions",
    "usage_historical_price_bindings",
    "usage_pricing_state",
    "usage_projection_state",
    "usage_valuations",
    "usage_session_totals",
    "usage_hourly_totals",
    "usage_daily_totals",
    "usage_maintenance_jobs",
    "provider_usage_samples",
    "usage_request_stats",
    "usage_request_logs",
    "usage_projection_outbox",
    "usage_coverage_epochs",
    "usage_archive_manifests",
    "usage_archived_totals",
)
