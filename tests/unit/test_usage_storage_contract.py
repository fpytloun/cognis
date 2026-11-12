"""Immutable population/dimension vocabulary and provider-semantic counterexamples."""

import json
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from cognis.core.usage.dimensions import (
    USAGE_METRIC_POPULATIONS,
    UsageDimensions,
    decode_usage_dimensions,
    encode_usage_dimensions,
    request_metric_values,
)
from cognis.models.usage import (
    USAGE_OBSERVATION_MAX_BYTES,
    UsageClientLabels,
    UsageClientSpan,
    UsageCount,
    UsageMetadataReduction,
    UsageModelName,
    UsageParameterName,
    UsageRequestStats,
    UsageServerSpan,
)
from cognis.models.usage_normalization import normalize_usage_tokens
from tests.usage_fixtures import finalized_attempt
from tests.usage_rich_fixture import dense_usage_event


def test_dense_observation_retains_all_metadata_and_reports_measured_budget() -> None:
    event = dense_usage_event()
    size = len(event.model_dump_json(exclude_none=True, exclude_defaults=True).encode())
    context_size = len(
        event.context.model_dump_json(exclude_none=True, exclude_defaults=True).encode()
    )
    assert 4096 < size <= USAGE_OBSERVATION_MAX_BYTES == 8192
    assert event.context.metadata_reduction is None
    for snapshot in (
        event.context.selection.requested_parameters,
        event.context.selection.effective_parameters,
    ):
        assert all(
            snapshot.provenance_source(name)
            == (None if name in {"image_size", "image_quality"} else "adapter_override")
            for name in get_args(UsageParameterName)
        )
    assert event.context.attribution.resources is not None
    print(
        f"usage_dense_envelope_bytes={size}; context_bytes={context_size}; terminal_bytes={size - context_size}"
    )


def test_dense_maximum_model_names_fit_near_envelope_boundary() -> None:
    from cognis.models.usage import UsageAttemptFinalized

    data = dense_usage_event().model_dump()
    data["context"]["selection"]["requested_model"] = "r" * 512
    data["context"]["selection"]["effective_model"] = "m" * 512
    data["provider_reported_model"] = "p" * 512
    event = UsageAttemptFinalized.model_validate(data)
    size = len(event.model_dump_json(exclude_none=True, exclude_defaults=True).encode())
    assert 7800 < size <= 8192
    assert event.context.metadata_reduction is None
    print(f"usage_near_boundary_envelope_bytes={size}")


def test_model_names_are_distinct_from_internal_resource_ids() -> None:
    from pydantic import TypeAdapter

    from cognis.models.usage import UsageId

    assert TypeAdapter(UsageModelName).validate_python("模型/" + "m" * 180)
    with pytest.raises(ValidationError):
        TypeAdapter(UsageId).validate_python("m" * 180)
    for invalid in ("model\nsecret", "model\u202esecret", "m" * 513):
        with pytest.raises(ValidationError):
            TypeAdapter(UsageModelName).validate_python(invalid)


def test_units_and_operations_are_bounded_and_semantic() -> None:
    from cognis.models.usage import UsageNumericExtra

    milliseconds = UsageNumericExtra(
        name="input_audio_duration_ms", value=12345, unit="milliseconds"
    )
    assert milliseconds.effective_unit == "milliseconds"
    assert UsageNumericExtra(name="input_image_count", value=3, unit="images").value == 3
    with pytest.raises(ValidationError):
        UsageNumericExtra(name="input_image_count", value=3, unit="tokens")
    with pytest.raises(ValidationError):
        UsageNumericExtra(name="input_audio_duration_ms", value=12.5, unit="milliseconds")
    for operation in (
        "embed_batch",
        "remember_dedup",
        "evidence_semantic_equivalence",
        "fsck_security",
    ):
        assert UsageClientLabels(operation=operation).operation == operation
    assert UsageClientLabels(operation="external-unrecognized").operation is None
    assert len(get_args(UsageParameterName)) == 18
    with pytest.raises(ValidationError):
        UsageMetadataReduction(fields=("resources",))


def test_dimensions_roundtrip_and_authorization_grain() -> None:
    identity = finalized_attempt().context.attribution.identity
    dimensions = UsageDimensions(
        record_kind="attempt",
        usage_subject_key=identity.usage_subject_key,
        purpose="chat",
        operation="chat",
        session_id="session_private",
        provider_id="provider_a",
        effective_model="m" * 180,
        hook_name="Průběžné shrnutí",
    )
    encoded = encode_usage_dimensions(dimensions, grain="hourly")
    assert decode_usage_dimensions(encoded.encoded_json) == dimensions
    assert decode_usage_dimensions(json.loads(encoded.encoded_json)) == dimensions
    assert (
        encode_usage_dimensions(
            decode_usage_dimensions(encoded.encoded_json), grain="hourly"
        ).dimension_key
        == encoded.dimension_key
    )
    daily = encode_usage_dimensions(dimensions, grain="daily")
    assert daily.dimensions.session_id is None
    assert daily.dimensions.usage_subject_key == identity.usage_subject_key
    assert daily.dimension_key != encoded.dimension_key
    request = UsageDimensions(
        record_kind="request",
        usage_subject_key=identity.usage_subject_key,
        purpose="chat",
        operation="request",
    )
    assert request.provider_id is None and request.effective_model is None
    with pytest.raises(ValidationError):
        UsageDimensions.model_validate(request.model_dump() | {"provider_id": "provider_a"})
    known_binding = UsageDimensions.model_validate(
        request.model_dump()
        | {
            "request_allocation_provenance": "verified_attempt",
            "provider_id": "provider_a",
            "effective_model": "model_a",
        }
    )
    assert known_binding.provider_id == "provider_a"


def test_client_timing_cannot_increment_server_logical_outcome() -> None:
    event = finalized_attempt()
    now = datetime.now(UTC)
    common = dict(
        observation_id="stats_01",
        request_id=event.context.trace.request_id,
        request_started_at=now,
        observed_at=now,
        attribution=event.context.attribution,
        controller_owner_id="controller_a",
        coverage_epoch_id="coverage_a",
    )
    client = UsageRequestStats(
        **common,
        stats_scope="client",
        client=UsageClientSpan(source="authenticated_report", elapsed_ms=2000, ttft_ms=300),
    )
    only_client = request_metric_values(server=None, client=client)
    assert only_client.logical_completed_count == only_client.logical_error_count == 0
    assert only_client.client_elapsed_count == 1 and only_client.server_elapsed_count == 0
    server = UsageRequestStats(
        **common,
        stats_scope="server",
        logical_outcome="error",
        server=UsageServerSpan(elapsed_ms=1800, ttft_ms=200),
    )
    combined = request_metric_values(server=server, client=client)
    assert combined.logical_completed_count == combined.logical_error_count == 1
    assert combined.server_elapsed_ms_sum == 1800
    assert combined.client_elapsed_ms_sum == 2000
    for population in USAGE_METRIC_POPULATIONS:
        if population.clock_domain == "client":
            assert all(not name.startswith("logical_") for name in population.measures)


def test_anthropic_additional_cache_is_inclusive_analytics_but_disjoint_billing() -> None:
    usage = finalized_attempt().usage.model_dump()
    usage.update(
        input_tokens={"value": 10, "status": "reported", "source": "provider"},
        output_tokens={"value": 5, "status": "reported", "source": "provider"},
        cache_read_tokens={"value": 90, "status": "reported", "source": "provider"},
        cache_write_tokens={"status": "not_applicable"},
        reasoning_tokens={"status": "not_applicable"},
        cache_read_semantics="additional_to_input",
        cache_write_semantics="included_in_input",
        reasoning_semantics="included_in_output",
    )
    from cognis.models.usage import NormalizedUsage

    normalized = normalize_usage_tokens(NormalizedUsage.model_validate(usage))
    assert normalized.input.value == 100
    assert normalized.output.value == 5
    assert normalized.total.value == 105
    assert normalized.billable.input.value == 10
    assert normalized.billable.cache_read.value == 90
    usage.update(
        input_tokens={"value": 100, "status": "reported", "source": "provider"},
        cache_read_semantics="included_in_input",
    )
    included = normalize_usage_tokens(NormalizedUsage.model_validate(usage))
    assert included.input.value == 100 and included.billable.input.value == 10
    usage.update(
        reasoning_tokens={"value": 3, "status": "reported", "source": "provider"},
        reasoning_semantics="included_in_output",
    )
    assert normalize_usage_tokens(NormalizedUsage.model_validate(usage)).output.value == 5
    usage["reasoning_semantics"] = "additional_to_output"
    assert normalize_usage_tokens(NormalizedUsage.model_validate(usage)).output.value == 8
    unknown = UsageCount()
    assert unknown.value is None
