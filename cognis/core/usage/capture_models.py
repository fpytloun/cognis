"""Controller-only lifecycle and provider-history participants."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from cognis.models.usage import (
    USAGE_OBSERVATION_MAX_BYTES,
    UsageEnvelope,
    UsageId,
    UsageInteger,
    UsageModelName,
    UsageSubject,
    UsageTimestamp,
)

UsageSampleValue = Annotated[Decimal, Field(ge=0, max_digits=30, decimal_places=12)]


class UsageProviderSample(UsageEnvelope):
    """Existing provider refresh/header observation; never requests upstream I/O."""

    sample_id: UsageId
    provider_id: UsageId
    provider_owner: UsageSubject
    account_scope_id: UsageId
    configuration_generation: UsageId
    source: Literal["refresh", "response_header", "native_sdk"]
    observation_id: UsageId
    window_key: UsageId
    observed_at: UsageTimestamp
    reset_at: UsageTimestamp | None = None
    model_bucket: UsageModelName | None = None
    limit_value: UsageSampleValue | None = None
    remaining: UsageSampleValue | None = None
    utilization: UsageSampleValue | None = None
    sample_count: UsageInteger = 1


@dataclass(frozen=True)
class UsageWriterLimits:
    """Hard in-memory budgets shared by events and provider samples."""

    queue_items: int = 1024
    queue_bytes: int = 4 * 1024 * 1024
    batch_items: int = 64
    batch_bytes: int = 128 * 1024
    flush_seconds: float = 0.025
    replay_days: int = 30
    shutdown_seconds: float = 5.0
    db_attempts: int = 3

    def __post_init__(self) -> None:
        if not (
            1 <= self.queue_items <= 1024
            and USAGE_OBSERVATION_MAX_BYTES <= self.queue_bytes <= 4 * 1024 * 1024
            and 1 <= self.batch_items <= 64
            and USAGE_OBSERVATION_MAX_BYTES <= self.batch_bytes <= 128 * 1024
            and 0 <= self.flush_seconds <= 0.025
            and self.replay_days >= 1
            and 0 < self.shutdown_seconds <= 5
            and 1 <= self.db_attempts <= 3
        ):
            raise ValueError("Usage writer limits exceed the delivery budgets")


@dataclass(frozen=True)
class UsageDrainResult:
    """Coverage diagnostics, never a durability receipt for inference."""

    pending_items: int
    pending_bytes: int
    dropped_started: int
    dropped_finalized: int
    dropped_stats: int
    dropped_samples: int
    committed_batches: int
    canonical_payload_bytes: int
    metadata_reduced_events: int = 0
    writer_failed: bool = False
    writer_running: bool = False
    shutdown_pending: bool = False
