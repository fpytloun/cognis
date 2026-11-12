"""Frozen storage/query dimension and metric-population contract.

This module is pure. It owns the JSON vocabulary and hash; query services must
not parse owner_key, reconstruct dimension hashes, or infer missing allocation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import StringConstraints, model_validator

from cognis.models.usage import (
    UsageCostBasis,
    UsageDisplayLabel,
    UsageId,
    UsageInteger,
    UsageLabel,
    UsageMilliseconds,
    UsageModel,
    UsageModelName,
    UsageRequestStats,
    UsageSource,
    UsageSubject,
)

UsageDimensionGrain = Literal["session", "hourly", "daily", "archive"]
UsageRecordKind = Literal["attempt", "request"]
TRAFFIC_FIELDS = ("traffic_origin", "traffic_auth_type", "traffic_client_id", "traffic_key_id")
RESOURCE_DIMENSION_FIELDS = (
    "session_id",
    "conversation_id",
    "activity_scope_id",
    "task_id",
    "step_run_id",
)


class UsageDimensions(UsageModel):
    """Nullable fields mean genuinely unallocated, not an invented provider/model."""

    schema_version: Literal[1, 2] = 1
    traffic_origin: Literal["cognis", "api", "unknown"] = "unknown"
    traffic_auth_type: str | None = None
    traffic_client_id: UsageId | None = None
    traffic_key_id: UsageId | None = None
    record_kind: UsageRecordKind
    request_allocation_provenance: Literal["verified_attempt"] | None = None
    usage_subject_key: Annotated[str, StringConstraints(min_length=1, max_length=333)]
    resource_owner: UsageSubject | None = None
    project_id: UsageId | None = None
    agent_id: UsageId | None = None
    agent_profile_id: UsageId | None = None
    provider_id: UsageId | None = None
    effective_model: UsageModelName | None = None
    effective_effort: UsageLabel | None = None
    purpose: UsageLabel
    operation: UsageLabel
    service_id: UsageId | None = None
    service_operation: UsageLabel | None = None
    hook_id: UsageId | None = None
    hook_revision: UsageInteger | None = None
    hook_name: UsageDisplayLabel | None = None
    hook_type: UsageLabel | None = None
    streaming: bool | None = None
    source: UsageSource | None = None
    session_id: UsageId | None = None
    conversation_id: UsageId | None = None
    activity_scope_id: UsageId | None = None
    task_id: UsageId | None = None
    step_run_id: UsageId | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    basis: UsageCostBasis | None = None

    @model_validator(mode="after")
    def request_allocation(self) -> UsageDimensions:
        allocated = any(
            (
                self.provider_id,
                self.effective_model,
                self.effective_effort,
                self.source,
                self.streaming is not None,
            )
        )
        if (
            self.record_kind == "request"
            and allocated
            and self.request_allocation_provenance is None
        ):
            raise ValueError("Request E2E has no inferred physical-attempt allocation")
        if self.request_allocation_provenance is not None and (
            self.record_kind != "request"
            or self.provider_id is None
            or self.effective_model is None
        ):
            raise ValueError("A verified request allocation requires a real provider/model binding")
        return self


class UsageDimensionEncoding(UsageModel):
    """Exact canonical JSON is suitable for dimensions columns, not another schema."""

    dimension_key: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    dimensions: UsageDimensions
    encoded_json: str


def encode_usage_dimensions(
    dimensions: UsageDimensions, *, grain: UsageDimensionGrain
) -> UsageDimensionEncoding:
    """Hash sorted UTF-8 JSON including nulls/defaults; daily drops resource grain."""
    if grain == "daily":
        data = dimensions.model_dump()
        for name in RESOURCE_DIMENSION_FIELDS:
            data[name] = None
        dimensions = UsageDimensions.model_validate(data)
    data = dimensions.model_dump(mode="json")
    if dimensions.schema_version == 1:
        # Retained v1 receipts must subtract from their original hash, not v2.
        for name in TRAFFIC_FIELDS:
            data.pop(name)
    encoded = json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return UsageDimensionEncoding(
        dimension_key=hashlib.sha256(encoded.encode()).hexdigest(),
        dimensions=dimensions,
        encoded_json=encoded,
    )


def decode_usage_dimensions(value: Mapping[str, object] | str) -> UsageDimensions:
    """Parse the sole published dimension shape; extra/unknown fields fail closed."""
    if isinstance(value, str):
        return UsageDimensions.model_validate_json(value)
    return UsageDimensions.model_validate(value)


class UsageRequestMetrics(UsageModel):
    """One canonical logical request, never an extra provider attempt."""

    logical_completed_count: Literal[0, 1] = 0
    logical_error_count: Literal[0, 1] = 0
    server_elapsed_ms_sum: UsageMilliseconds = 0
    server_elapsed_count: Literal[0, 1] = 0
    server_ttft_ms_sum: UsageMilliseconds = 0
    server_ttft_count: Literal[0, 1] = 0
    client_elapsed_ms_sum: UsageMilliseconds = 0
    client_elapsed_count: Literal[0, 1] = 0
    client_ttft_ms_sum: UsageMilliseconds = 0
    client_ttft_count: Literal[0, 1] = 0


def request_metric_values(
    *, server: UsageRequestStats | None, client: UsageRequestStats | None
) -> UsageRequestMetrics:
    """Only authoritative server evidence can set logical outcome counters."""
    if server is not None and server.stats_scope != "server":
        raise ValueError("Server population requires a server statistics observation")
    if client is not None and client.stats_scope != "client":
        raise ValueError("Client population requires a client statistics observation")
    if server is not None and client is not None and server.request_id != client.request_id:
        raise ValueError("Cannot combine statistics from different logical requests")
    values: dict[str, int | float] = {}
    if server is not None and server.logical_outcome is not None:
        values["logical_completed_count"] = 1
        values["logical_error_count"] = int(server.logical_outcome == "error")
    for prefix, observation in (("server", server), ("client", client)):
        if observation is None:
            continue
        span = observation.server if prefix == "server" else observation.client
        if span is None:
            continue
        for name in ("elapsed", "ttft"):
            value = getattr(span, f"{name}_ms")
            if value is not None:
                values[f"{prefix}_{name}_ms_sum"] = value
                values[f"{prefix}_{name}_count"] = 1
    return UsageRequestMetrics.model_validate(values)


class UsageMetricPopulation(UsageModel):
    """Add only compatible scalar families, never blanket-sum record kinds."""

    record_kind: UsageRecordKind
    clock_domain: Literal["provider", "server", "client"]
    denominator: Literal[
        "finalized_count",
        "logical_completed_count",
        "server_elapsed_count",
        "server_ttft_count",
        "client_elapsed_count",
        "client_ttft_count",
        "total_elapsed_count",
        "dispatch_elapsed_count",
        "provider_elapsed_count",
        "ttft_count",
        "generation_elapsed_count",
        "generation_output_count",
    ]
    measures: tuple[str, ...]


USAGE_METRIC_POPULATIONS = (
    (
        UsageMetricPopulation(
            record_kind="attempt",
            clock_domain="provider",
            denominator="finalized_count",
            measures=(
                "finalized_count",
                "success_count",
                "error_count",
                "cancelled_count",
                "not_dispatched_count",
                "unknown_outcome_count",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
            ),
        ),
        UsageMetricPopulation(
            record_kind="request",
            clock_domain="server",
            denominator="logical_completed_count",
            measures=("logical_completed_count", "logical_error_count"),
        ),
        UsageMetricPopulation(
            record_kind="request",
            clock_domain="server",
            denominator="server_elapsed_count",
            measures=("server_elapsed_ms_sum", "server_elapsed_count"),
        ),
        UsageMetricPopulation(
            record_kind="request",
            clock_domain="server",
            denominator="server_ttft_count",
            measures=("server_ttft_ms_sum", "server_ttft_count"),
        ),
        UsageMetricPopulation(
            record_kind="request",
            clock_domain="client",
            denominator="client_elapsed_count",
            measures=("client_elapsed_ms_sum", "client_elapsed_count"),
        ),
        UsageMetricPopulation(
            record_kind="request",
            clock_domain="client",
            denominator="client_ttft_count",
            measures=("client_ttft_ms_sum", "client_ttft_count"),
        ),
    )
    + tuple(
        UsageMetricPopulation.model_validate(
            {
                "record_kind": "attempt",
                "clock_domain": "provider",
                "denominator": f"{name}_count",
                "measures": (f"{name}_ms_sum", f"{name}_count"),
            }
        )
        for name in (
            "total_elapsed",
            "dispatch_elapsed",
            "provider_elapsed",
            "ttft",
            "generation_elapsed",
        )
    )
    + (
        UsageMetricPopulation(
            record_kind="attempt",
            clock_domain="provider",
            denominator="generation_output_count",
            measures=(
                "generation_output_tokens",
                "generation_output_ms",
                "generation_output_count",
            ),
        ),
    )
)


def usage_cost_basis_to_api(
    value: UsageCostBasis,
) -> Literal["api_estimate", "configured_equivalent", "provider_reported"]:
    """Subscription/API-equivalent estimates are never presented as invoices."""
    if value == "configured_estimate":
        return "api_estimate"
    if value == "provider_reported":
        return "provider_reported"
    return "configured_equivalent"


def usage_milliseconds_to_seconds(value: float | None) -> float | None:
    """Convert only an already measured duration, never subtract clock domains."""
    return value / 1000 if value is not None else None


def usage_outcome_to_api(
    value: Literal["success", "error", "cancelled", "not_dispatched", "unknown"],
) -> Literal["succeeded", "failed", "cancelled", "unknown"]:
    """Unobserved/non-dispatched outcomes are not fabricated successes or errors."""
    if value == "success":
        return "succeeded"
    if value == "error":
        return "failed"
    if value == "cancelled":
        return "cancelled"
    return "unknown"
