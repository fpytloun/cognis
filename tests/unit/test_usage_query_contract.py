"""Usage wire contracts, sufficient statistics and pure forecasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cognis.core.usage.forecast import forecast_usage
from cognis.core.usage.query_models import (
    MAX_SAFE_INTEGER,
    CostSubtotal,
    ForecastOptions,
    TokenCoverage,
    TokenTotals,
    UsageAttemptDetail,
    UsageAttemptPage,
    UsageAvailability,
    UsageBucket,
    UsageCaptureCoverage,
    UsageMeta,
    UsageMetrics,
    UsagePerformance,
    UsagePrivateAttemptDetail,
    UsageProjectionCoverage,
    UsageQuery,
    UsageScope,
    UsageSummaryRequest,
)

START = datetime(2026, 10, 1, tzinfo=UTC)
NOW = START + timedelta(days=2)
OPTIONS = ForecastOptions(horizon_seconds=86400)


def _buckets(*, priced: bool = True) -> list[UsageBucket]:
    return [
        UsageBucket(
            start=START + timedelta(hours=hour),
            end=START + timedelta(hours=hour + 1),
            complete=True,
            capture=UsageCaptureCoverage(state="observed"),
            projection=UsageProjectionCoverage(state="ready"),
            source_grain="hour",
            metrics=UsageMetrics(
                finalized_attempts=1,
                observed_attempts=1,
                pending_complete=True,
                tokens=TokenTotals(input=100, output=10, cache_read=0),
                token_known_attempts=TokenCoverage(input=1, output=1, cache_read=1),
                costs=[
                    CostSubtotal(
                        known_amount="0.000000001",
                        currency="USD",
                        basis="api_estimate",
                        quality="complete",
                        priced_attempts=1,
                        unpriced_attempts=0,
                    )
                ]
                if priced
                else [],
            ),
        )
        for hour in range(24)
    ]


def test_forecast_independent_tokens_and_exact_cost() -> None:
    result = forecast_usage(_buckets(), bucket="hour", options=OPTIONS, now=NOW)
    assert result.tokens["input"].projected_tokens == 2400
    assert result.tokens["output"].projected_tokens == 240
    assert result.tokens["cache_read"].projected_tokens == 0
    assert result.tokens["reasoning"].reason == "unknown"
    assert result.cost.available
    assert result.cost.projected_cost is not None
    assert Decimal(result.cost.projected_cost.known_amount) == Decimal("0.000000024")

    result = forecast_usage(_buckets(priced=False), bucket="hour", options=OPTIONS, now=NOW)
    assert result.tokens["input"].available
    assert result.cost.reason == "unpriced"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("gap", "capture_gap"),
        ("unknown", "capture_unknown"),
        ("lagging", "projection_lag"),
        ("building", "projection_lag"),
        ("unknown_grain", "unsupported_resolution"),
        ("day_grain", "unsupported_resolution"),
        ("archive_hour", "unsupported_resolution"),
    ],
)
def test_capture_coverage_and_projection_lag_are_independent(change: str, reason: str) -> None:
    buckets = _buckets()
    if change == "gap":
        buckets[0].capture = UsageCaptureCoverage(state="gaps", dropped_observations=2)
    elif change == "unknown":
        buckets[0].capture = UsageCaptureCoverage()
    elif change == "lagging":
        buckets[0].projection = UsageProjectionCoverage(
            state="lagging", unprojected_observations=2, lag_seconds=3
        )
    elif change == "building":
        buckets[0].projection = UsageProjectionCoverage(state="building")
    elif change == "unknown_grain":
        buckets[0].source_grain = None
    elif change == "day_grain":
        buckets[0].source_grain = "day"
    else:
        buckets[0].source = "archive"
    result = forecast_usage(buckets, bucket="hour", options=OPTIONS, now=NOW)
    assert result.cost.reason == reason
    assert result.tokens["input"].reason == reason


def test_archived_daily_accounting_supports_observed_forecast_without_detail() -> None:
    buckets = _buckets()[:7]
    for index, item in enumerate(buckets):
        item.start = START + timedelta(days=index)
        item.end = item.start + timedelta(days=1)
        item.source_grain = "day"
        item.source = "archive"
    result = forecast_usage(buckets, bucket="day", options=OPTIONS, now=START + timedelta(days=8))
    assert result.tokens["input"].projected_tokens == 100
    assert result.cost.available
    assert result.cost.projected_cost.known_amount == "0.000000001"
    assert any("capture" in text for text in result.limitations)


def test_forecast_rounding_multiplies_before_division() -> None:
    buckets = _buckets()
    for item in buckets:
        item.metrics.tokens.input = 1800
        item.metrics.costs[0].known_amount = "1"
    result = forecast_usage(
        buckets, bucket="hour", options=ForecastOptions(horizon_seconds=1), now=NOW
    )
    assert result.tokens["input"].projected_tokens == 1  # 43200 / 86400 rounds half-up
    assert result.cost.projected_cost.known_amount == "0.0002777777777777777777777777778"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("short", "insufficient"),
        ("gap", "sparse"),
        ("partial", "partial"),
        ("pending", "partial"),
        ("unchecked_pending", "partial"),
        ("future", "partial"),
        ("unaligned", "partial"),
        ("duplicate", "sparse"),
    ],
)
def test_forecast_rejects_incomplete_windows(change: str, reason: str) -> None:
    buckets = _buckets()
    now = NOW
    if change == "short":
        buckets.pop()
    elif change == "gap":
        buckets[-1].start += timedelta(hours=1)
        buckets[-1].end += timedelta(hours=1)
    elif change == "partial":
        buckets[0].complete = False
    elif change == "pending":
        buckets[0].metrics.observed_attempts = 2
    elif change == "unchecked_pending":
        buckets[0].metrics.pending_complete = None
    elif change == "future":
        now = START
    elif change == "unaligned":
        buckets[0].start += timedelta(minutes=1)
    elif change == "duplicate":
        buckets[-1] = buckets[-2]
    result = forecast_usage(buckets, bucket="hour", options=OPTIONS, now=now)
    assert result.cost.reason == reason
    assert all(value.reason == reason for value in result.tokens.values())


@pytest.mark.parametrize("change", ["currency", "basis", "unpriced", "unknown_tokens"])
def test_forecast_coverage_is_independent(change: str) -> None:
    buckets = _buckets()
    if change == "currency":
        buckets[0].metrics.costs[0].currency = "EUR"
    elif change == "basis":
        buckets[0].metrics.costs[0].basis = "configured_equivalent"
    elif change == "unpriced":
        buckets[0].metrics.costs[0].quality = "partial"
    else:
        buckets[0].metrics.token_known_attempts.input = 0
    result = forecast_usage(buckets, bucket="hour", options=OPTIONS, now=NOW)
    if change == "unknown_tokens":
        assert result.cost.available
        assert result.tokens["input"].reason == "unknown"
        assert result.tokens["output"].available
    else:
        assert result.tokens["input"].available
        assert (
            result.cost.reason
            == {"currency": "mixed_currency", "basis": "mixed_basis", "unpriced": "unpriced"}[
                change
            ]
        )


def test_forecast_token_overflow_is_unavailable() -> None:
    buckets = _buckets()
    for item in buckets:
        item.metrics.tokens.input = MAX_SAFE_INTEGER
    result = forecast_usage(buckets, bucket="hour", options=OPTIONS, now=NOW)
    assert result.tokens["input"].reason == "overflow"


def test_certified_empty_buckets_and_zero_price_are_not_unknown() -> None:
    buckets = _buckets()
    for item in buckets[:20]:
        item.metrics = UsageMetrics(observed_attempts=0, pending_complete=True)
    for item in buckets[20:]:
        item.metrics.costs[0].known_amount = "0"
    result = forecast_usage(buckets, bucket="hour", options=OPTIONS, now=NOW)
    assert result.tokens["input"].projected_tokens == 400
    assert result.cost.available
    assert result.cost.projected_cost.known_amount == "0"
    buckets[20].metrics.finalized_attempts = 0
    buckets[20].metrics.observed_attempts = 0
    buckets[21].metrics.finalized_attempts = 0
    buckets[21].metrics.observed_attempts = 0
    result = forecast_usage(buckets, bucket="hour", options=OPTIONS, now=NOW)
    assert result.cost.reason == "sparse"


def test_sparse_daily_and_naive_forecast_time() -> None:
    buckets = _buckets()[:7]
    for index, item in enumerate(buckets):
        item.start = START + timedelta(days=index)
        item.end = item.start + timedelta(days=1)
    result = forecast_usage(buckets, bucket="day", options=OPTIONS, now=START + timedelta(days=8))
    assert result.tokens["input"].projected_tokens == 100
    with pytest.raises(ValueError, match="timezone"):
        forecast_usage(buckets, bucket="day", options=OPTIONS, now=NOW.replace(tzinfo=None))


def test_performance_rates_use_matching_weighted_populations() -> None:
    stats = UsagePerformance(
        ttft_known_samples=2,
        ttft_seconds_sum=1.0,
        latency_known_samples=3,
        latency_seconds_sum=12.0,
        generation_known_samples=2,
        generation_seconds_sum=5.0,
        generation_output_tokens=150,
        end_to_end_known_samples=2,
        end_to_end_seconds_sum=10.0,
        end_to_end_output_tokens=150,
        dispatched_attempts=3,
        failed_dispatched_attempts=1,
        retry_attempts=1,
        logical_completed_requests=2,
        logical_failed_requests=0,
    )
    wire = stats.model_dump(mode="json")
    assert wire["ttft_seconds_mean"] == 0.5
    assert wire["latency_seconds_mean"] == 4
    assert wire["generation_tokens_per_second"] == 30
    assert wire["end_to_end_tokens_per_second"] == 15
    assert wire["attempt_failure_rate"] == 1 / 3
    assert wire["logical_failure_rate"] == 0
    assert UsagePerformance().attempt_failure_rate is None
    assert UsagePerformance().logical_failure_rate is None
    with pytest.raises(ValidationError):
        UsagePerformance(ttft_seconds_sum=0)
    with pytest.raises(ValidationError):
        UsagePerformance(failed_dispatched_attempts=1)


def test_query_and_sidebar_budgets_and_unknowns() -> None:
    query = UsageQuery(start=START, end=NOW)
    assert query.scope.mode == "personal"
    assert not query.include_pending
    assert UsageMetrics().observed_attempts is None
    assert UsageMetrics().tokens.input is None
    for overrides in (
        {"end": START},
        {"end": START + timedelta(days=91), "bucket": "hour"},
        {"group_by": "sql_expression"},
        {"arbitrary_sql": "select 1"},
        {"start": START.replace(tzinfo=None)},
        {"limit": 1001},
    ):
        with pytest.raises(ValidationError):
            UsageQuery.model_validate({"start": START, "end": NOW, **overrides})
    assert UsageQuery(
        start=START, end=NOW, filters={"key_ids": ["key_a"]}, group_by="key"
    ).filters.key_ids == ["key_a"]
    with pytest.raises(ValidationError):
        UsageScope(mode="project")
    with pytest.raises(ValidationError):
        UsageSummaryRequest(items=[{"kind": "session", "id": "session_a"}] * 101)
    with pytest.raises(ValidationError):
        TokenTotals(input=MAX_SAFE_INTEGER + 1)
    with pytest.raises(ValidationError):
        CostSubtotal(
            known_amount="NaN",
            currency="USD",
            basis="api_estimate",
            quality="unknown",
            priced_attempts=0,
            unpriced_attempts=1,
        )


def test_team_schema_excludes_private_provenance_and_raw_error_summary() -> None:
    values = {
        "attempt_id": "attempt_a",
        "state": "failed",
        "subject_email": "member@usage.test",
        "started_at": START,
        "tokens": {},
        "usage_quality": "unknown",
        "costs": [],
        "error_category": "provider",
    }
    detail = UsageAttemptDetail.model_validate(values)
    assert detail.error_summary == "Provider reported an error."
    assert detail.ttft_seconds is None
    for forbidden in (
        "key_id",
        "client_id",
        "client_request_id",
        "provider_request_id",
        "server_request_id",
        "error_summary",
    ):
        assert forbidden not in UsageAttemptDetail.model_fields
        with pytest.raises(ValidationError):
            UsageAttemptDetail.model_validate({**values, forbidden: "secret"})
    private = UsagePrivateAttemptDetail.model_validate({**values, "key_id": "key_a"})
    assert private.key_id == "key_a"
    meta = UsageMeta(
        scope=UsageScope(mode="project", project_id="proj_usage"),
        precision="exact",
        complete=True,
        capture=UsageCaptureCoverage(state="observed"),
        availability=UsageAvailability(resolution="retained"),
        projection=UsageProjectionCoverage(state="ready"),
    )
    with pytest.raises(ValidationError, match="reduced"):
        UsageAttemptPage(meta=meta, items=[private])
    assert UsageAttemptPage(meta=meta, items=[detail]).items[0] == detail
    assert "cash" not in str(CostSubtotal.model_json_schema())
