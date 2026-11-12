"""Typed summary measures with an explicit producer population contract."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Text, cast, func

from cognis.core.usage.dimensions import (
    USAGE_METRIC_POPULATIONS,
    UsageDimensions,
    decode_usage_dimensions,
)
from cognis.core.usage.query_adapters import BASIS, seconds
from cognis.core.usage.query_models import (
    CostSubtotal,
    UsageCacheMetrics,
    UsageDimension,
    UsageDurationStats,
    UsageLogicalPerformance,
    UsageMetrics,
    UsagePerformance,
    UsageStates,
)
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.usage.models import UsageSummaryMeasures

MEASURES = tuple(UsageSummaryMeasures.__annotations__)


class UsagePopulationContract:
    """The frozen v1 encoder, not caller-configurable storage semantics."""

    archived_dimensions: tuple[UsageDimension, ...] = (
        "traffic_origin",
        "auth_type",
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
        "task",
        "schedule",
    )
    inclusive_tokens_confirmed = True
    logical_populations_confirmed = True

    def record_kind(self, source: Any) -> Any:
        """One fixed, trusted encoder key; callers cannot provide SQL expressions."""
        assert "record_kind" in UsageDimensions.model_fields
        return source.dimensions["record_kind"].as_string()


def aggregate_columns(source: Any) -> list[Any]:
    """Typed measures and a bounded canonical dimension witness, never raw facts."""
    version = source.dimensions["schema_version"].as_string()
    return [
        *[func.sum(getattr(source, name)).label(name) for name in MEASURES],
        func.min(cast(source.dimensions, Text)).label("canonical_dimensions"),
        func.min(version).label("min_dimension_version"),
        func.max(version).label("max_dimension_version"),
        func.count(version).label("versioned_dimension_count"),
        func.count().label("dimension_count"),
    ]


def merge_measures(rows: list[Any]) -> dict[str, Any]:
    """Combine already bounded summary results, not raw facts."""
    for row in rows:
        dimensions = decode_usage_dimensions(row["canonical_dimensions"])
        if (
            str(row["min_dimension_version"]) != str(dimensions.schema_version)
            or str(row["max_dimension_version"]) != str(dimensions.schema_version)
            or row["versioned_dimension_count"] != row["dimension_count"]
        ):
            raise UsageQueryError(
                "usage_source_incompatible", "Mixed or unversioned summary dimensions", 503
            )
    return {name: sum((row[name] or 0 for row in rows), Decimal(0)) for name in MEASURES}


def metrics(rows: list[Any], *, basis: str, contract: UsagePopulationContract) -> UsageMetrics:
    """Attempt contributions only. Request statistics are never added as attempts."""
    if not rows:
        # A certified aggregate SELECT can establish an observed empty population.
        return UsageMetrics(
            finalized_attempts=0,
            unpriced_attempts=0,
            partially_priced_attempts=0,
            unallocatable_cost_attempts=0,
        )
    sums = merge_measures(rows)
    count = int(sums["finalized_count"])
    token_values: dict[str, int | None] = {}
    token_known: dict[str, int] = {}
    for field in ("input", "output", "cache_read", "cache_write"):
        known = int(sums[f"{field}_known_count"])
        if known > count:
            raise UsageQueryError(
                "usage_population_mismatch", "Token population exceeds attempts", 503
            )
        token_known[field] = known if contract.inclusive_tokens_confirmed else 0
        token_values[field] = (
            int(sums[f"{field}_tokens"]) if known and contract.inclusive_tokens_confirmed else None
        )
    # The committed schema has no reasoning-known population. Do not invent one.
    token_values["reasoning"] = None
    token_known["reasoning"] = 0
    costs = []
    currencies: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        if row["currency"] and row["basis"] in BASIS:
            currencies.setdefault((row["currency"], row["basis"]), []).append(row)
    for (currency, cost_basis), population in currencies.items():
        values = merge_measures(population)
        priced = int(values[f"{basis}_priced_count"])
        partial = int(values[f"{basis}_partial_count"])
        attempts = int(values["finalized_count"])
        costs.append(
            CostSubtotal(
                known_amount=format(values[f"{basis}_cost"], "f") if priced or partial else None,
                currency=currency,
                basis=BASIS[cost_basis],
                quality="complete"
                if priced == attempts
                else "partial"
                if priced or partial
                else "unknown",
                priced_attempts=priced,
                unpriced_attempts=attempts - priced,
                partially_priced_attempts=partial,
            )
        )
    # Adapter-observed attempts start at provider dispatch and end at the final
    # response, so their total elapsed time is the provider span. A separately
    # reported provider span, when present, stays authoritative.
    latency_count, latency_ms = (
        ("provider_elapsed_count", "provider_elapsed_ms_sum")
        if sums["provider_elapsed_count"]
        else ("total_elapsed_count", "total_elapsed_ms_sum")
    )
    performance = UsagePerformance(
        ttft_known_samples=int(sums["ttft_count"]),
        ttft_seconds_sum=seconds(sums["ttft_ms_sum"]) if sums["ttft_count"] else None,
        latency_known_samples=int(sums[latency_count]),
        latency_seconds_sum=seconds(sums[latency_ms]) if sums[latency_count] else None,
        generation_known_samples=int(sums["generation_output_count"]),
        generation_seconds_sum=seconds(sums["generation_output_ms"])
        if sums["generation_output_count"]
        else None,
        generation_output_tokens=int(sums["generation_output_tokens"])
        if sums["generation_output_count"]
        else None,
        # Whole-attempt throughput: known output over the same attempts' duration.
        end_to_end_known_samples=int(sums[latency_count]) if sums["output_known_count"] else 0,
        end_to_end_seconds_sum=seconds(sums[latency_ms])
        if sums[latency_count] and sums["output_known_count"]
        else None,
        end_to_end_output_tokens=int(sums["output_tokens"])
        if sums[latency_count] and sums["output_known_count"]
        else None,
        # Errors include failures before dispatch; the ratio is only stated when
        # every error is known to belong to a dispatched attempt.
        **(
            {
                "dispatched_attempts": int(sums["dispatch_observed_count"]),
                "failed_dispatched_attempts": int(sums["error_count"]),
            }
            if sums["error_count"] <= sums["dispatch_observed_count"]
            else {}
        ),
    )
    return UsageMetrics(
        finalized_attempts=count,
        unpriced_attempts=count - int(sums[f"{basis}_priced_count"]),
        partially_priced_attempts=int(sums[f"{basis}_partial_count"]),
        unallocatable_cost_attempts=count
        - sum(int(merge_measures(values)["finalized_count"]) for values in currencies.values()),
        states=UsageStates(
            succeeded=int(sums["success_count"]),
            failed=int(sums["error_count"]),
            cancelled=int(sums["cancelled_count"]),
            unknown=int(sums["unknown_outcome_count"] + sums["not_dispatched_count"]),
        ),
        tokens=token_values,
        token_known_attempts=token_known,
        costs=costs,
        reported_attempts=int(sums["reported_count"]),
        partial_attempts=int(sums["partial_count"]),
        unknown_usage_attempts=int(sums["unknown_usage_count"]),
        performance=performance,
        prompt_cache=UsageCacheMetrics(
            eligible_requests=int(sums["cache_eligible_count"])
            if sums["cache_eligibility_known_count"] == count
            else None,
            hit_requests=int(sums["cache_read_positive_count"])
            if sums["cache_read_known_count"] == count
            else None,
            cache_read_tokens=int(sums["cache_read_tokens"])
            if sums["cache_read_known_count"] == count
            else None,
        ),
    )


def logical_metrics(rows: list[Any], contract: UsagePopulationContract) -> UsageLogicalPerformance:
    """Logical timer means require the confirmed matched-population encoder."""
    if not contract.logical_populations_confirmed or not rows:
        return UsageLogicalPerformance()
    values = merge_measures(rows)

    def duration(count: str) -> UsageDurationStats:
        spec = next(
            spec
            for spec in USAGE_METRIC_POPULATIONS
            if spec.record_kind == "request" and spec.denominator == count
        )
        samples = int(values[spec.denominator])
        return UsageDurationStats(
            known_samples=samples,
            seconds_sum=seconds(values[spec.measures[0]]) if samples else None,
        )

    return UsageLogicalPerformance(
        server_boundary=duration("server_elapsed_count"),
        server_first_send=duration("server_ttft_count"),
        # v1 request_metric_values mixes authenticated and unverified reports.
        # A mixed cohort cannot qualify authenticated client receipt timings.
    )


def combined_metrics(
    attempts: list[Any],
    requests: list[Any],
    *,
    basis: str,
    contract: UsagePopulationContract,
) -> UsageMetrics:
    """Keep physical accounting and once-only logical observations separate."""
    result = metrics(attempts, basis=basis, contract=contract)
    result.logical_performance = logical_metrics(requests, contract)
    if requests:
        values = merge_measures(requests)
        completed = int(values["logical_completed_count"])
        if completed:
            result.performance = UsagePerformance.model_validate(
                result.performance.model_dump(exclude_computed_fields=True)
                | {
                    "logical_completed_requests": completed,
                    "logical_failed_requests": int(values["logical_error_count"]),
                }
            )
    return result
