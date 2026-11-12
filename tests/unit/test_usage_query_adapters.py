"""Canonical scalar provenance and disjoint summary-population regressions."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cognis.core.usage.dimensions import (
    UsageDimensions,
    encode_usage_dimensions,
    request_metric_values,
)
from cognis.core.usage.query_adapters import attempt_detail, parameters
from cognis.core.usage.query_models import UsageFilters, UsageParameterSnapshot
from cognis.core.usage.query_populations import (
    MEASURES,
    UsagePopulationContract,
    combined_metrics,
)
from cognis.models.usage import NormalizedUsage, UsageCount, UsageRequestStats
from cognis.store.usage.codec import context_values, terminal_values
from cognis.store.usage.models import UsageAttemptRow
from tests.usage_fixtures import attempt_context, finalized_attempt


def aggregate(kind, **values):
    context = attempt_context()
    encoding = encode_usage_dimensions(
        UsageDimensions(
            record_kind=kind,
            usage_subject_key=context.attribution.identity.usage_subject_key,
            purpose=context.attribution.purpose,
            operation="chat" if kind == "attempt" else "request",
            provider_id=context.selection.provider_id if kind == "attempt" else None,
            effective_model=context.selection.effective_model if kind == "attempt" else None,
        ),
        grain="daily",
    )
    return {
        **dict.fromkeys(MEASURES, 0),
        # Numeric aggregate columns are decoded as Decimal by SQLAlchemy.
        **{
            name: Decimal(str(value)) if isinstance(value, float) else value
            for name, value in values.items()
        },
        "canonical_dimensions": encoding.encoded_json,
        "min_dimension_version": str(encoding.dimensions.schema_version),
        "max_dimension_version": str(encoding.dimensions.schema_version),
        "versioned_dimension_count": 1,
        "dimension_count": 1,
        "currency": None,
        "basis": None,
    }


def stats(**values):
    context = attempt_context()
    return UsageRequestStats(
        observation_id="query_population_observation",
        request_id=context.trace.request_id,
        request_started_at=context.started_at,
        observed_at=context.started_at + timedelta(seconds=1),
        controller_owner_id=context.controller_owner_id,
        coverage_epoch_id=context.coverage_epoch_id,
        attribution=context.attribution,
        **values,
    )


def test_default_provenance_and_exceptions_use_full_lookup():
    result = parameters(
        {
            "temperature": 0.2,
            "top_p": 0.9,
            "reasoning_budget": 5000,
            "reasoning_enabled": True,
            "provenance_default": "profile",
            "provenance": [{"parameter": "top_p", "source": "request"}],
        }
    )
    assert result.temperature.provenance == "configured_default"
    assert result.top_p.provenance == "explicit"
    assert result.reasoning_budget_tokens.value == 5000
    assert result.reasoning_enabled.value is True
    result.model_dump_json(warnings="error")


def test_observed_scalar_without_provenance_is_not_a_default():
    result = parameters({"temperature": 0.2})
    assert result.temperature.value == 0.2
    assert result.temperature.provenance == "observed_unspecified"


def test_generic_defaults_serialize_without_union_warnings():
    UsageParameterSnapshot().model_dump_json(warnings="error")


def test_unicode_model_limits_are_independent_of_resource_ids():
    model = "模式" * 256
    assert UsageFilters(model_ids=[model]).model_ids == [model]
    with pytest.raises(ValidationError):
        UsageFilters(model_ids=[model + "a"])


def test_server_completion_is_once_only_not_last_attempt_inference():
    server = stats(stats_scope="server", logical_outcome="success", server={"elapsed_ms": 1000})
    logical = aggregate("request", **request_metric_values(server=server, client=None).model_dump())
    physical = aggregate("attempt", finalized_count=3, success_count=2, error_count=1)
    result = combined_metrics(
        [physical],
        [logical],
        basis="historical",
        contract=UsagePopulationContract(),
    )
    assert result.finalized_attempts == 3
    assert result.performance.logical_completed_requests == 1
    assert result.performance.logical_failed_requests == 0
    assert result.performance.logical_failure_rate == 0
    assert result.logical_performance.server_boundary.known_samples == 1
    assert result.logical_performance.server_boundary.seconds_mean == 1


def test_unverified_client_does_not_claim_completion_or_authenticated_e2e():
    client = stats(
        stats_scope="client",
        client={"source": "unverified_report", "elapsed_ms": 2000},
    )
    logical = aggregate("request", **request_metric_values(server=None, client=client).model_dump())
    result = combined_metrics(
        [],
        [logical],
        basis="historical",
        contract=UsagePopulationContract(),
    )
    assert result.performance.logical_completed_requests is None
    assert result.performance.logical_failed_requests is None
    assert result.logical_performance.authenticated_client.last_chunk.seconds_sum is None


@pytest.mark.parametrize(
    ("semantics", "tokens", "throughput"),
    [("additional_to_output", 8, 8), ("unknown", 5, None)],
)
def test_detail_throughput_uses_fully_known_inclusive_output(semantics, tokens, throughput):
    event = finalized_attempt()
    event = event.model_copy(
        update={
            "usage": NormalizedUsage.model_validate(
                {
                    **event.usage.model_dump(),
                    "completeness": "reported",
                    "reasoning_semantics": semantics,
                    "input_tokens": UsageCount(value=10, status="reported", source="provider"),
                    "output_tokens": UsageCount(value=5, status="reported", source="provider"),
                    "reasoning_tokens": UsageCount(value=3, status="reported", source="provider"),
                }
            ),
            "performance": event.performance.model_copy(
                update={
                    "generation_elapsed_ms": 1000.0,
                    "provider_elapsed_ms": 1100.0,
                    "total_elapsed_ms": 1200.0,
                }
            ),
            "finalized_at": event.context.started_at + timedelta(milliseconds=1200),
        }
    )
    row = UsageAttemptRow(
        **(
            context_values(event.context, event.context.started_at)
            | terminal_values(event, event.finalized_at)
        )
    )
    row.terminal_payload = event.model_dump(mode="json", exclude={"context"})
    result = attempt_detail(row, None, None, private=False, basis="historical")
    assert result.tokens.output == tokens
    assert result.generation_tokens_per_second == throughput
    if semantics == "unknown":
        assert result.usage_quality == "partial"


@pytest.mark.parametrize(
    ("status", "amount", "priced"),
    [("complete", "0.125", 1), ("complete", "0", 1), ("partial", "0.1", 0), ("unknown", "0", None)],
)
def test_detail_cost_preserves_valuation_status_and_explicit_zero(status, amount, priced):
    event = finalized_attempt()
    row = UsageAttemptRow(
        **(
            context_values(event.context, event.context.started_at)
            | terminal_values(event, event.finalized_at)
        )
    )
    valuation = SimpleNamespace(
        historical_cost=Decimal(amount),
        historical_status=status,
        currency="USD",
        basis="configured_estimate",
    )
    result = attempt_detail(row, valuation, None, private=False, basis="historical")
    if priced is None:
        assert result.costs == []
    else:
        assert Decimal(result.costs[0].known_amount) == Decimal(amount)
        assert result.costs[0].quality == status
        assert result.costs[0].priced_attempts == priced
        assert result.costs[0].unpriced_attempts == 1 - priced


@pytest.mark.parametrize(
    ("known", "eligible", "hits"),
    [(1, 0, 0), (1, 1, 1), (0, 0, 0)],
)
def test_cache_request_counts_require_known_populations(known, eligible, hits):
    row = aggregate(
        "attempt",
        finalized_count=1,
        cache_eligibility_known_count=known,
        cache_eligible_count=eligible,
        cache_read_known_count=known,
        cache_read_positive_count=hits,
        cache_read_tokens=hits * 4,
    )
    result = combined_metrics([row], [], basis="historical", contract=UsagePopulationContract())
    assert result.prompt_cache.eligible_requests == (eligible if known else None)
    assert result.prompt_cache.hit_requests == (hits if known else None)
    assert result.prompt_cache.eligible_input_tokens is None


@pytest.mark.parametrize("provider_span", [False, True])
def test_performance_uses_observed_attempt_span_failures_and_throughput(provider_span):
    row = aggregate(
        "attempt",
        finalized_count=4,
        dispatch_observed_count=4,
        error_count=1,
        output_known_count=3,
        output_tokens=300,
        total_elapsed_count=4,
        total_elapsed_ms_sum=8000.0,
        provider_elapsed_count=2 if provider_span else 0,
        provider_elapsed_ms_sum=3000.0 if provider_span else 0,
    )
    performance = combined_metrics(
        [row], [], basis="historical", contract=UsagePopulationContract()
    ).performance
    # Without a separately reported provider span, the adapter-observed attempt
    # duration is the provider span; a reported span stays authoritative.
    assert performance.latency_known_samples == (2 if provider_span else 4)
    assert performance.latency_seconds_mean == pytest.approx(1.5 if provider_span else 2.0)
    assert performance.dispatched_attempts == 4
    assert performance.failed_dispatched_attempts == 1
    assert performance.end_to_end_output_tokens == 300
    assert performance.end_to_end_seconds_sum == pytest.approx(3.0 if provider_span else 8.0)
