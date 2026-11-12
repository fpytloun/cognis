from __future__ import annotations

import inspect
from datetime import datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from cognis.models.usage import (
    UsageAttemptSink,
    UsageAttemptStarted,
    UsageClientSpan,
    UsageCount,
    UsageExecutorContext,
    UsageModelParameters,
    UsageObservation,
    UsageParameterProvenance,
    UsageRequestStats,
    UsageSafeError,
    UsageServerSpan,
)
from tests.usage_fixtures import OBSERVED_AT, attempt_context, finalized_attempt


def test_capture_is_synchronous_and_has_no_durable_admission_surface() -> None:
    assert not inspect.iscoroutinefunction(UsageAttemptSink.try_emit)
    assert not hasattr(UsageAttemptSink, "prepare")
    assert not hasattr(UsageAttemptSink, "finalize")


def test_finalized_is_self_contained_without_start_or_receipt() -> None:
    observation = finalized_attempt()
    decoded = TypeAdapter(UsageObservation).validate_json(observation.model_dump_json())
    assert decoded == observation
    assert decoded.context.trace.attempt_id == "attempt_usage_contract"
    assert decoded.context.attribution.identity.usage_subject_id == "usage+test@cognis.test"
    assert decoded.usage.input_tokens.value is None
    assert decoded.performance.ttft_ms is None
    assert len(decoded.model_dump_json(exclude_none=True, exclude_defaults=True).encode()) < 4096
    with pytest.raises(ValidationError):
        observation.outcome = "error"


def test_started_context_is_immutable_across_executor_transport() -> None:
    context = attempt_context().model_dump()
    context["executor"] = UsageExecutorContext(
        executor_id="executor_usage_contract",
        controller_owner_id="controller_usage_contract",
        inference_request_id="executor_request_usage_contract",
        connection_epoch=2,
    )
    event = UsageAttemptStarted(
        observation_id="observation_usage_start",
        context=context,
    )
    decoded = TypeAdapter(UsageObservation).validate_json(event.model_dump_json())
    assert decoded == event
    assert decoded.context.executor.inference_request_id == "executor_request_usage_contract"
    assert event.observation_digest() == decoded.observation_digest()


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_counts_reject_invalid_numeric_values(value: object) -> None:
    with pytest.raises(ValidationError):
        UsageCount(value=value, status="reported", source="provider")


def test_unknown_partial_and_zero_are_distinct() -> None:
    assert UsageCount().value is None
    assert UsageCount(value=0, status="reported", source="provider").value == 0
    assert UsageCount(value=3, status="partial", source="native_sdk").status == "partial"
    with pytest.raises(ValidationError):
        UsageCount(value=0, status="unknown")
    with pytest.raises(ValidationError):
        UsageCount(status="reported")
    with pytest.raises(ValidationError):
        UsageCount(value=2, status="reported")


def test_parameters_have_provenance_and_no_payload_escape_hatch() -> None:
    parameters = UsageModelParameters(
        temperature=0,
        top_p=0.9,
        top_k=10,
        max_output_tokens=4096,
        seed=-2,
        frequency_penalty=0.2,
        presence_penalty=0.1,
        repetition_penalty=1.1,
        response_format="json_schema",
        tool_choice="named",
        provenance=(UsageParameterProvenance(parameter="temperature", source="request"),),
    )
    assert isinstance(parameters.provenance, tuple)
    for payload in ({"stop": ("secret",)}, {"tools": ()}, {"response_schema": {}}):
        with pytest.raises(ValidationError):
            UsageModelParameters(**payload)
    with pytest.raises(ValidationError):
        UsageModelParameters(temperature=float("nan"))
    with pytest.raises(ValidationError):
        UsageModelParameters(provenance=parameters.provenance * 2)


def test_errors_are_safe_template_codes_not_exception_bodies() -> None:
    error = UsageSafeError(
        category="timeout",
        summary_code="provider_timeout",
        exception_class="ReadTimeout",
        provider_error_code="request_timeout",
        http_status=504,
    )
    assert error.summary_code == "provider_timeout"
    with pytest.raises(ValidationError):
        UsageSafeError(category="unknown", summary_code="provider_error", raw_error="prompt")
    with pytest.raises(ValidationError):
        UsageSafeError(category="unknown", summary_code="provider_error", exception_class="a\nb")


def test_server_and_client_spans_are_separate_clock_domains() -> None:
    base = dict(
        observation_id="stats_usage_contract",
        request_id="req_usage_contract",
        request_started_at=OBSERVED_AT,
        observed_at=OBSERVED_AT,
        attribution=attempt_context().attribution,
        controller_owner_id="controller_usage_contract",
        coverage_epoch_id="coverage_usage_contract",
    )
    server = UsageRequestStats(
        **base,
        stats_scope="server",
        logical_outcome="success",
        server=UsageServerSpan(first_sent_at=OBSERVED_AT, elapsed_ms=100),
    )
    assert server.client is None
    assert server.server.ttft_ms is None  # Total response latency is not TTFT.
    client = UsageRequestStats(
        **base,
        stats_scope="client",
        client=UsageClientSpan(source="authenticated_report", elapsed_ms=140, ttft_ms=50),
    )
    assert client.server is None
    with pytest.raises(ValidationError):
        UsageRequestStats(
            **base,
            stats_scope="client",
            client=client.client,
            logical_outcome="success",
        )
    with pytest.raises(ValidationError):
        UsageRequestStats(**base, stats_scope="server", server=server.server, client=client.client)


def test_naive_times_and_oversized_observations_are_rejected() -> None:
    event = finalized_attempt().model_dump()
    event["finalized_at"] = datetime(2026, 10, 29)
    with pytest.raises(ValidationError):
        TypeAdapter(UsageObservation).validate_python(event)
    event = finalized_attempt().model_dump()
    labels = {
        "operation_id": "a" * 160,
        "client_request_id": "b" * 160,
        "project_id": "c" * 160,
        "session_id": "d" * 160,
        "task_id": "e" * 160,
    }
    event["context"]["attribution"]["client_labels"] = labels
    event["context"]["attribution"]["resources"] = {
        "provenance": "controller",
        **{
            key: "x" * 160
            for key in (
                "project_id",
                "conversation_id",
                "session_id",
                "activity_scope_id",
                "task_id",
                "step_run_id",
                "turn_id",
                "agent_id",
                "agent_profile_id",
            )
        },
    }
    event["provider_request_id"] = "y" * 160
    event["provider_reported_model"] = "z" * 160
    event["context"]["attribution"]["hook"] = {
        "hook_id": "h" * 160,
        "hook_name": "n" * 128,
        "hook_type": "t" * 128,
        "invocation_id": "i" * 160,
        "occurrence": "o" * 160,
    }
    identity = event["context"]["attribution"]["identity"]
    identity.update(
        principal_subject="s" * 320,
        usage_subject_id="u" * 320,
        initiating_actor="a" * 320,
        resource_owner="r" * 320,
        provider_payer="p" * 320,
        routing_owner="o" * 320,
        client_id="c" * 160,
        key_record_id="k" * 160,
        token_record_id="t" * 160,
    )
    event["context"]["selection"]["requested_model"] = "r" * 512
    event["context"]["selection"]["effective_model"] = "m" * 512
    event["provider_reported_model"] = "p" * 512
    with pytest.raises(ValidationError, match="8 KiB"):
        TypeAdapter(UsageObservation).validate_python(event)
