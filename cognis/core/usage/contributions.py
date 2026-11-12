"""Pure versioned contributions shared by projection, rebuild and archival."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from cognis.core.usage.dimensions import (
    UsageDimensionGrain,
    UsageDimensions,
    encode_usage_dimensions,
)
from cognis.core.usage.pricing import UsagePriceResult, calculate_price, valuation_domain
from cognis.core.usage.sessions import (
    UsageSessionCorrelation,
    session_correlation,
    session_dimension_key,
)
from cognis.models.usage import UsagePriceDefinition
from cognis.models.usage_normalization import normalize_usage_tokens
from cognis.models.usage_traffic import traffic_dimensions
from cognis.store.usage.codec import terminal_event
from cognis.store.usage.models import UsageAttemptRow, UsageSummaryMeasures

MEASURES = tuple(UsageSummaryMeasures.__annotations__)
RESOURCE_FIELDS = ("session_id", "conversation_id", "activity_scope_id", "task_id", "step_run_id")
MONEY_MEASURES = frozenset(
    name
    for name in MEASURES
    if name.endswith("_sum") or name in {"historical_cost", "current_cost", "generation_output_ms"}
)


def digest_json(value: Any) -> str:
    """Hash deterministic content-free dimensions/contributions."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def empty_measures() -> dict[str, int | Decimal]:
    """Every summary shape has exactly the same bounded scalar measure vocabulary."""
    return {name: Decimal(0) if name in MONEY_MEASURES else 0 for name in MEASURES}


@dataclass(frozen=True)
class UsageContribution:
    """An applied receipt contains old deltas, not another copy of the raw event."""

    owner_key: str
    day: date
    hour: int
    dimensions: dict[str, Any]
    measures: dict[str, int | Decimal]
    units: dict[str, int]
    historical: UsagePriceResult
    current: UsagePriceResult
    session: UsageSessionCorrelation | None = None

    def payload(self) -> dict[str, Any]:
        """Persist only sufficient reversible additive state."""
        payload = {
            "owner_key": self.owner_key,
            "day": self.day.isoformat(),
            "hour": self.hour,
            "dimensions": self.dimensions,
            "measures": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in self.measures.items()
            },
            "units": self.units,
        }
        if self.session is not None:
            payload["session"] = self.session.model_dump(mode="json")
        return payload


def attempt_contribution(
    row: UsageAttemptRow,
    historical_price: UsagePriceDefinition | None,
    current_price: UsagePriceDefinition | None,
) -> UsageContribution:
    """Normalize exactly once at the canonical provider semantic boundary."""
    event = terminal_event(row)
    historical = calculate_price(event.usage, event.context.selection, historical_price)
    current = calculate_price(event.usage, event.context.selection, current_price)
    # A mixed revision currency/basis must never be combined into a misleading scalar.
    if (
        historical.cost is not None
        and current.cost is not None
        and (historical.currency != current.currency or historical.basis != current.basis)
    ):
        raise ValueError("Repricing cannot silently change currency or cost basis")
    measures = empty_measures()
    measures["finalized_count"] = 1
    outcome = {
        "success": "success_count",
        "error": "error_count",
        "cancelled": "cancelled_count",
        "not_dispatched": "not_dispatched_count",
        "unknown": "unknown_outcome_count",
    }
    measures[outcome[event.outcome]] = 1
    measures["dispatch_observed_count"] = int(
        event.dispatch_status in {"dispatch_observed", "provider_accepted"}
    )
    measures["provider_accepted_count"] = int(event.dispatch_status == "provider_accepted")
    measures[
        {
            "reported": "reported_count",
            "partial": "partial_count",
            "unknown": "unknown_usage_count",
            "not_applicable": "unknown_usage_count",
        }[event.usage.completeness]
    ] = 1
    normalized = normalize_usage_tokens(event.usage)
    for name in ("input", "output", "cache_read", "cache_write", "reasoning"):
        count = getattr(normalized, name)
        measures[f"{name}_tokens"] = count.value or 0
        if name in {"input", "output", "cache_read", "cache_write"}:
            measures[f"{name}_known_count"] = int(count.value is not None)
        if name in {"cache_read", "cache_write"}:
            measures[f"{name}_positive_count"] = int(bool(count.value))
            measures[f"{name}_unknown_count"] = int(count.status == "unknown")
    measures["cache_eligible_count"] = int(event.usage.cache_eligible is True)
    measures["cache_eligibility_known_count"] = int(event.usage.cache_eligible is not None)
    for prefix, result in (("historical", historical), ("current", current)):
        measures[f"{prefix}_cost"] = result.cost or Decimal(0)
        measures[f"{prefix}_priced_count"] = int(result.status == "complete")
        measures[f"{prefix}_partial_count"] = int(result.status == "partial")
    performance = event.performance
    for name in (
        "total_elapsed",
        "dispatch_elapsed",
        "provider_elapsed",
        "ttft",
        "generation_elapsed",
    ):
        value = getattr(performance, f"{name}_ms")
        if value is not None:
            measures[f"{name}_ms_sum"] = Decimal(str(value))
            measures[f"{name}_count"] = 1
    if (
        event.usage.output_tokens.status == "reported"
        and event.usage.output_tokens.value is not None
        and performance.generation_elapsed_ms is not None
        and performance.generation_elapsed_ms > 0
    ):
        measures["generation_output_tokens"] = event.usage.output_tokens.value
        measures["generation_output_ms"] = Decimal(str(performance.generation_elapsed_ms))
        measures["generation_output_count"] = 1
    fields = (
        "usage_subject_key",
        "resource_owner",
        "project_id",
        "agent_id",
        "agent_profile_id",
        "provider_id",
        "effective_model",
        "effective_effort",
        "purpose",
        "operation",
        "service_id",
        "service_operation",
        "hook_id",
        "hook_type",
        "streaming",
        "source",
        *RESOURCE_FIELDS,
    )
    dimensions = {key: getattr(row, key) for key in fields}
    dimensions.update(schema_version=2, **traffic_dimensions(event.context.attribution))
    currency, basis = valuation_domain(historical, current)
    dimensions.update(
        hook_revision=row.hook_revision,
        hook_name=row.hook_name,
        currency=currency,
        basis=basis,
        record_kind="attempt",
    )
    dimensions = UsageDimensions.model_validate(dimensions).model_dump(mode="json")
    return UsageContribution(
        row.owner_key,
        row.bucket_day,
        event.context.started_at.hour,
        dimensions,
        measures,
        dict(current.units),
        historical,
        current,
        session_correlation(event.context.attribution),
    )


def decode_measures(payload: dict[str, Any]) -> dict[str, int | Decimal]:
    """The sole decimal decoding rule for persisted delta receipts."""
    return {
        name: Decimal(payload["measures"][name])
        if name in MONEY_MEASURES
        else int(payload["measures"][name])
        for name in MEASURES
    }


def summary_rows(
    contributions: list[tuple[dict[str, Any], int]],
    *,
    generation: int,
    grain: UsageDimensionGrain,
    now: datetime,
) -> list[dict[str, Any]]:
    """Collapse all same-key changes before a single table UPSERT."""
    grouped: dict[tuple[str, str, str, int | None], dict[str, Any]] = {}
    for payload, sign in contributions:
        if grain == "hourly" and payload.get("grain") == "daily":
            continue
        dimensions = dict(payload["dimensions"])
        correlation = None
        if grain == "session":
            if payload.get("session") is not None:
                correlation = UsageSessionCorrelation.model_validate(payload["session"])
            elif dimensions.get("session_id") is not None:
                correlation = UsageSessionCorrelation(
                    session_kind="cognis", session_id=dimensions["session_id"]
                )
            if correlation is None:
                continue
        encoding = encode_usage_dimensions(UsageDimensions.model_validate(dimensions), grain=grain)
        dimensions = encoding.dimensions.model_dump(mode="json")
        dimension_key = encoding.dimension_key
        if correlation is not None:
            dimension_key = session_dimension_key(encoding.dimensions, correlation)
        hour = payload["hour"] if grain == "hourly" else None
        key = (payload["owner_key"], payload["day"], dimension_key, hour)
        values = grouped.get(key)
        if values is None:
            values = {
                **dimensions,
                "owner_key": payload["owner_key"],
                "bucket_day": date.fromisoformat(payload["day"]),
                "generation": generation,
                "dimension_key": dimension_key,
                "dimensions": dimensions,
                "updated_at": now,
                **empty_measures(),
            }
            if hour is not None:
                values["bucket_hour"] = hour
            if correlation is not None:
                values.update(correlation.model_dump())
            grouped[key] = values
        for name, value in decode_measures(payload).items():
            values[name] += value * sign
    return list(grouped.values())
