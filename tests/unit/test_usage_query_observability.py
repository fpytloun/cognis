"""Async coverage, safe parameters, clock boundaries and retention fidelity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from cognis.core.usage.availability import UsageResolutionError, check_query_availability
from cognis.core.usage.query_models import (
    UsageAttemptDetail,
    UsageAttemptPage,
    UsageAvailability,
    UsageCaptureCoverage,
    UsageClientTiming,
    UsageDurationStats,
    UsageLogicalPerformance,
    UsageLogicalTiming,
    UsageMeta,
    UsageParameterSnapshot,
    UsageProjectionCoverage,
    UsageQuery,
    UsageRequestParameters,
    UsageScope,
    UsageServerTiming,
    UsageSummaryItem,
)

START = datetime(2026, 9, 1, tzinfo=UTC)
CUTOFF = START + timedelta(days=30)


def _detail(**overrides) -> UsageAttemptDetail:
    return UsageAttemptDetail.model_validate(
        {
            "attempt_id": "attempt_observed",
            "subject_email": "member@usage.test",
            "state": "observed_start",
            "started_at": START,
            "tokens": {},
            "usage_quality": "unknown",
            "costs": [],
            **overrides,
        }
    )


def _archive(**overrides) -> UsageAvailability:
    return UsageAvailability.model_validate(
        {
            "resolution": "aggregate_only",
            "detail_retention_cutoff": CUTOFF,
            "archive_grain": "day",
            **overrides,
        }
    )


def test_parameter_snapshots_preserve_requested_effective_and_missing_provenance() -> None:
    snapshots = UsageRequestParameters.model_validate(
        {
            "requested": {
                "temperature": {"value": 0, "provenance": "explicit"},
                "top_p": {"value": 0.9, "provenance": "configured_default"},
                "max_tokens": {"value": 128, "provenance": "explicit"},
                "response_format": {"value": "json_schema", "provenance": "explicit"},
            },
            "effective": {
                "temperature": {"value": 1, "provenance": "adapter_normalized"},
                "top_p": {"provenance": "unsupported"},
                "seed": {"provenance": "provider_default"},
            },
        }
    )
    assert snapshots.requested.temperature.value == 0
    assert snapshots.effective.temperature.value == 1
    assert snapshots.effective.top_p.value is None
    assert snapshots.effective.seed.value is None
    assert snapshots.effective.tool_choice.provenance == "unknown"
    for invalid in (
        {"temperature": {"value": 1, "provenance": "unknown"}},
        {"temperature": {"value": 1, "provenance": "unsupported"}},
        {"temperature": {"value": float("nan"), "provenance": "explicit"}},
        {"top_p": {"value": 2, "provenance": "explicit"}},
        {"max_tokens": {"value": True, "provenance": "explicit"}},
        {"temperature": {"provenance": "explicit"}},
        {"stop": ["payload"]},
        {"response_schema": {"secret": "payload"}},
        {"tool_definitions": []},
        {"tool_choice": {"value": {"name": "secret"}, "provenance": "explicit"}},
    ):
        with pytest.raises(ValidationError):
            UsageParameterSnapshot.model_validate(invalid)


def test_observed_start_is_not_dispatch_or_durable_admission() -> None:
    detail = _detail()
    assert detail.dispatched_at is None
    assert detail.ttft_seconds is None
    assert detail.parameters.effective.temperature.value is None
    with pytest.raises(ValidationError):
        _detail(state="prepared")
    with pytest.raises(ValidationError):
        _detail(prepared_at=START)
    assert UsageCaptureCoverage().state == "unknown"
    assert UsageCaptureCoverage().hard_crash_loss_possible
    with pytest.raises(ValidationError):
        UsageCaptureCoverage(state="observed", dropped_observations=1)
    with pytest.raises(ValidationError):
        UsageProjectionCoverage(state="ready", unprojected_observations=1)


def test_server_provider_and_client_clocks_never_imply_one_another() -> None:
    server = UsageServerTiming.model_validate(
        {
            "elapsed_seconds": 5,
            "first_send_seconds": 1.2,
            "last_send_seconds": 5,
            "partition_complete": True,
            "spans": [
                {"kind": "routing", "start_seconds": 0, "end_seconds": 1},
                {"kind": "provider_wait", "start_seconds": 1, "end_seconds": 4},
                {"kind": "finalization", "start_seconds": 4, "end_seconds": 5},
            ],
        }
    )
    client = UsageClientTiming(
        source="authenticated_client_report",
        correlation_verified=True,
        first_chunk_seconds=12,
        last_chunk_seconds=15,
    )
    detail = _detail(
        state="succeeded",
        latency_seconds=3,
        provider_timing_source="executor_monotonic",
        logical_timing=UsageLogicalTiming(server=server, client=client),
    )
    assert detail.latency_seconds == 3
    assert detail.logical_timing.server.cognis_overhead_seconds == 2
    assert detail.logical_timing.client.last_chunk_seconds == 15
    assert UsageLogicalTiming(server=server).client is None
    assert "client_receipt" not in server.model_dump()
    assert UsageServerTiming(elapsed_seconds=5).cognis_overhead_seconds is None
    with pytest.raises(ValidationError):
        UsageServerTiming(source="executor_monotonic", elapsed_seconds=5)
    with pytest.raises(ValidationError):
        UsageClientTiming(source="authenticated_client_report", correlation_verified=False)
    with pytest.raises(ValidationError):
        UsageClientTiming(
            source="authenticated_client_report",
            correlation_verified=True,
            first_chunk_seconds=3,
            last_chunk_seconds=1,
        )
    with pytest.raises(ValidationError):
        UsageClientTiming(source="cognis_ui", correlation_verified=True, last_chunk_seconds=86401)


def test_overhead_requires_nonoverlapping_complete_controller_partition() -> None:
    for spans in (
        [
            {"kind": "routing", "start_seconds": 0, "end_seconds": 2},
            {"kind": "provider_wait", "start_seconds": 1, "end_seconds": 5},
        ],
        [
            {"kind": "routing", "start_seconds": 0, "end_seconds": 1},
            {"kind": "provider_wait", "start_seconds": 2, "end_seconds": 5},
        ],
    ):
        with pytest.raises(ValidationError, match="partition"):
            UsageServerTiming(elapsed_seconds=5, spans=spans, partition_complete=True)
        assert UsageServerTiming(elapsed_seconds=5, spans=spans).cognis_overhead_seconds is None
    remote = UsageServerTiming.model_validate(
        {
            "elapsed_seconds": 5,
            "partition_complete": True,
            "spans": [{"kind": "executor_wait", "start_seconds": 0, "end_seconds": 5}],
        }
    )
    assert remote.cognis_overhead_seconds is None
    with pytest.raises(ValidationError):
        UsageServerTiming(elapsed_seconds=5, last_send_seconds=6)


def test_nonstream_ttft_and_logical_sample_populations() -> None:
    with pytest.raises(ValidationError, match="Nonstream"):
        _detail(streaming=False, ttft_seconds=1, provider_timing_source="controller_monotonic")
    assert (
        _detail(
            streaming=False, ttft_seconds=1, provider_timing_source="provider_reported"
        ).ttft_seconds
        == 1
    )
    logical = UsageLogicalPerformance(
        server_boundary=UsageDurationStats(known_samples=2, seconds_sum=10),
        cognis_overhead=UsageDurationStats(known_samples=1, seconds_sum=1),
    )
    assert logical.server_boundary.seconds_mean == 5
    assert logical.cognis_overhead.seconds_mean == 1
    assert logical.authenticated_client.last_chunk.seconds_mean is None


def test_expired_attempt_page_is_explicit_not_an_empty_zero_claim() -> None:
    meta = UsageMeta(
        scope=UsageScope(),
        precision="day",
        complete=False,
        availability=_archive(resolution="detail_expired"),
    )
    assert UsageAttemptPage(meta=meta, items=[]).meta.availability.detail_retention_cutoff == CUTOFF
    for overrides in ({"items": [_detail()]}, {"items": [], "next_cursor": "cursor_retained"}):
        with pytest.raises(ValidationError, match="Expired"):
            UsageAttemptPage(meta=meta, **overrides)
    with pytest.raises(ValidationError, match="cutoff"):
        UsageAvailability(resolution="detail_expired")
    assert UsageAvailability().resolution == "unavailable"
    assert _archive().configured_detail_days is None  # proposed default is not live config


def test_unavailable_sidebar_selector_cannot_leak_retention_or_session_membership() -> None:
    selector = {"kind": "session", "id": "session_unknown"}
    item = UsageSummaryItem(selector=selector, available=False, inclusion="self")
    assert item.availability.resolution == "unavailable"
    assert item.physical_sessions is None
    for extra in (
        {"physical_sessions": 1},
        {"metrics": {}},
        {"availability": _archive()},
    ):
        with pytest.raises(ValidationError, match="evidence"):
            UsageSummaryItem(selector=selector, available=False, inclusion="self", **extra)
    with pytest.raises(ValidationError, match="known capture"):
        UsageMeta(
            scope=UsageScope(),
            precision="day",
            complete=True,
            availability=UsageAvailability(resolution="retained"),
        )


def test_archive_query_uses_daily_authorized_accounting_without_repricing_invention() -> None:
    values = {"start": START, "end": START + timedelta(days=2)}
    available = _archive(
        historical_repricing="unsupported",
        repricing_unavailable_reason="unsupported_per_request_rules",
        unsupported_fields=["parameters", "cycle_ids", "profile"],
    )
    check_query_availability(UsageQuery(**values), available)
    for overrides, code in (
        ({"bucket": "hour"}, "unsupported_historic_resolution"),
        ({"start": START + timedelta(hours=1)}, "unsupported_historic_resolution"),
        ({"filters": {"cycle_ids": ["cycle_old"]}}, "unsupported_historic_filter"),
        ({"group_by": "profile"}, "unsupported_historic_filter"),
        ({"pricing_basis": "current"}, "unsupported_historic_repricing"),
    ):
        with pytest.raises(UsageResolutionError) as error:
            check_query_availability(UsageQuery.model_validate({**values, **overrides}), available)
        assert error.value.code == code
    with pytest.raises(UsageResolutionError) as error:
        check_query_availability(UsageQuery(**values), UsageAvailability())
    assert error.value.code == "usage_source_unavailable"
    supported = _archive(historical_repricing="supported")
    check_query_availability(UsageQuery(**values, pricing_basis="current"), supported)
    assert available.stored_valuation_preserved
    with pytest.raises(UsageResolutionError):
        check_query_availability(UsageQuery(**values, view="companions"), available)
    check_query_availability(
        UsageQuery(**values, view="companions"),
        _archive(supported_dimensions=["service", "operation"]),
    )


@pytest.mark.parametrize(
    "injected",
    [
        {"actor": "owner@usage.test"},
        {"capture": {"state": "observed"}},
        {"availability": {"resolution": "aggregate_only"}},
        {"correlation_verified": True},
        {"disable_accounting": True},
        {"parameters": {"temperature": 0}},
        {"filters": {"temperature": 0}},
    ],
)
def test_observation_metadata_and_headers_cannot_self_authorize_queries(injected: dict) -> None:
    with pytest.raises(ValidationError):
        UsageQuery.model_validate(
            {
                "start": START,
                "end": START + timedelta(days=1),
                **injected,
            }
        )
