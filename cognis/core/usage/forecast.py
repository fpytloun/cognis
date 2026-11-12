"""Bounded constant-rate extrapolation from already-read closed usage buckets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from cognis.core.usage.query_models import (
    MAX_SAFE_INTEGER,
    CostPrediction,
    CostSubtotal,
    ForecastOptions,
    ForecastReason,
    TokenPrediction,
    UsageBucket,
    UsageForecast,
)

_TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")
_LIMITATIONS = [
    "Constant observed rate; no seasonality, confidence interval or billing prediction.",
    "Whole-token projections use round-half-up; costs retain decimal arithmetic.",
    "Trusted attempt-start buckets can receive late observations; incomplete buckets are excluded.",
    "Asynchronous capture can lose observations; this is an observed-rate forecast, not total billing.",
]


def forecast_usage(
    buckets: list[UsageBucket],
    *,
    bucket: Literal["hour", "day"],
    options: ForecastOptions,
    now: datetime,
) -> UsageForecast:
    """Project independently covered token fields and one compatible cost basis.

    The caller supplies current time. No I/O, new sampling, cache or timer occurs.
    Every supplied bucket must be closed, aligned, contiguous and complete.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Forecast time must be timezone-aware")
    now = now.astimezone(UTC)
    ordered = sorted(buckets, key=lambda item: item.start)
    reason = _window_reason(ordered, bucket=bucket, now=now)
    seconds = sum((item.end - item.start).total_seconds() for item in ordered)
    with localcontext() as context:
        context.prec = 128
        sample_seconds = Decimal(str(seconds))
        tokens = {
            field: _token_prediction(
                ordered,
                field=field,
                horizon_seconds=options.horizon_seconds,
                sample_seconds=sample_seconds,
                reason=reason,
            )
            for field in _TOKEN_FIELDS
        }
        cost = _cost_prediction(
            ordered,
            horizon_seconds=options.horizon_seconds,
            sample_seconds=sample_seconds,
            reason=reason,
        )
    return UsageForecast(
        sample_start=ordered[0].start if ordered else None,
        sample_end=ordered[-1].end if ordered else None,
        horizon_seconds=options.horizon_seconds,
        tokens=tokens,
        cost=cost,
        limitations=list(_LIMITATIONS),
    )


def _window_reason(
    buckets: list[UsageBucket], *, bucket: Literal["hour", "day"], now: datetime
) -> ForecastReason | None:
    minimum = 24 if bucket == "hour" else 7
    duration = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    if len(buckets) < minimum:
        return "insufficient"
    if len(buckets) > 1000:
        raise ValueError("Forecast exceeds the interactive bucket budget")
    for index, item in enumerate(buckets):
        start = item.start.astimezone(UTC)
        end = item.end.astimezone(UTC)
        aligned = start.minute == start.second == start.microsecond == 0
        if bucket == "day":
            aligned = aligned and start.hour == 0
        if not aligned or end - start != duration:
            return "partial"
        if end > now or not item.complete:
            return "partial"
        if index and buckets[index - 1].end != item.start:
            return "sparse"
        if item.source_grain is None or (item.source_grain == "day" and bucket == "hour"):
            return "unsupported_resolution"
        if item.source in {"archive", "mixed"} and bucket == "hour":
            return "unsupported_resolution"
        if item.capture.state == "gaps":
            return "capture_gap"
        if item.capture.state != "observed":
            return "capture_unknown"
        if (
            item.projection.state != "ready"
            or item.projection.unprojected_observations
            or item.projection.lag_seconds
        ):
            return "projection_lag"
        if item.metrics.pending_complete is not True:
            return "partial"
        if (
            item.metrics.observed_attempts is None
            or item.metrics.observed_attempts != item.metrics.finalized_attempts
        ):
            return "partial"
    if sum(item.metrics.finalized_attempts > 0 for item in buckets) < 3:
        return "sparse"
    return None


def _token_prediction(
    buckets: list[UsageBucket],
    *,
    field: str,
    horizon_seconds: int,
    sample_seconds: Decimal,
    reason: ForecastReason | None,
) -> TokenPrediction:
    if reason:
        return TokenPrediction(available=False, reason=reason)
    total = 0
    for item in buckets:
        metrics = item.metrics
        known = getattr(metrics.token_known_attempts, field)
        value = getattr(metrics.tokens, field)
        # An empty bucket is zero only after _window_reason certifies coverage.
        if known != metrics.finalized_attempts or (value is None and metrics.finalized_attempts):
            return TokenPrediction(available=False, reason="unknown")
        total += value or 0
    projected = int(
        (Decimal(total) * horizon_seconds / sample_seconds).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    if projected > MAX_SAFE_INTEGER:
        return TokenPrediction(available=False, reason="overflow")
    return TokenPrediction(available=True, projected_tokens=projected)


def _cost_prediction(
    buckets: list[UsageBucket],
    *,
    horizon_seconds: int,
    sample_seconds: Decimal,
    reason: ForecastReason | None,
) -> CostPrediction:
    if reason:
        return CostPrediction(available=False, reason=reason)
    costs = [cost for item in buckets for cost in item.metrics.costs]
    if not costs:
        return CostPrediction(available=False, reason="unpriced")
    if len({cost.currency for cost in costs}) != 1:
        return CostPrediction(available=False, reason="mixed_currency")
    if len({cost.basis for cost in costs}) != 1:
        return CostPrediction(available=False, reason="mixed_basis")
    for item in buckets:
        if item.metrics.finalized_attempts == 0 and not item.metrics.costs:
            continue
        if (
            len(item.metrics.costs) != 1
            or item.metrics.costs[0].quality != "complete"
            or item.metrics.costs[0].known_amount is None
            or item.metrics.costs[0].unpriced_attempts
            or item.metrics.costs[0].priced_attempts != item.metrics.finalized_attempts
        ):
            return CostPrediction(available=False, reason="unpriced")
    total = sum((Decimal(cost.known_amount or "0") for cost in costs), Decimal(0))
    # Multiply exact sample values first. Rounding a recurring rate before
    # multiplication can corrupt exact ties and manufacture string overflow.
    numerator = total * horizon_seconds
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        amount = format(numerator / sample_seconds, "f")
    if len(amount) > 100:
        return CostPrediction(available=False, reason="overflow")
    return CostPrediction(
        available=True,
        projected_cost=CostSubtotal(
            known_amount=amount,
            currency=costs[0].currency,
            basis=costs[0].basis,
            quality="complete",
            priced_attempts=sum(cost.priced_attempts for cost in costs),
            unpriced_attempts=0,
        ),
    )
