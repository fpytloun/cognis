"""Physical executor submissions, frozen correlation, and transport loss evidence."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from cognis.executor.inference import InferenceHandler
from cognis.executor.usage_observer import ExecutorUsageObserver, executor_observer
from cognis.models.usage import UsageAttemptFinalized, UsageAttemptStarted, UsageObservation
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_executor import executor_metadata, ingest_executor
from cognis.providers.llm.usage_http import observed_http
from tests.unit.test_usage_capture_runtime import Sink, request
from tests.unit.test_usage_factory import capture_provider as capture_provider


def remote_scope(*, model="gpt-test"):
    sink = Sink()
    scope = request(sink)
    binding = UsageBinding(scope, "provider-test", model, "payer@example.com")
    metadata, context = executor_metadata(
        binding,
        None,
        executor_id="executor-test",
        parameters={},
        streaming=False,
    )
    observer = executor_observer(metadata)
    assert isinstance(observer, ExecutorUsageObserver)
    assert context is not None
    assert sink.offers == []
    return sink, binding, context, observer


@pytest.mark.parametrize("remaining", [{}, {"backend_duration_s": 0.5}])
def test_ingestion_removes_only_the_private_observation_envelope(remaining):
    encoded = {
        "result": True,
        "backend_metadata": {**remaining, "cognis_usage_observations": []},
    }
    expected = {"result": True}
    if remaining:
        expected["backend_metadata"] = remaining
    assert ingest_executor(encoded, None, None) == expected


async def test_executor_physical_sdk_calls_are_not_an_outer_rpc_attempt(capture_provider):
    sink, binding, context, observer = remote_scope(model="gpt-4o-mini")
    endpoint, calls = capture_provider
    result = await InferenceHandler().generate(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "PRIVATE PROMPT"}],
        request_kwargs={
            "cognis_llm_api": "responses",
            "api_base": endpoint,
            "api_key": "capture-fixture-key",
            "num_retries": 0,
            "max_retries": 0,
        },
        usage_observer=observer,
    )
    offered = result["backend_metadata"]["cognis_usage_observations"]
    assert [event["kind"] for event in offered] == ["attempt_started", "attempt_finalized"]
    assert all(TypeAdapter(UsageObservation).validate_python(event) for event in offered)
    result = ingest_executor(result, binding, context)
    assert calls == ["/v1/responses"]
    assert len(sink.offers) == 2
    start, final = sink.offers
    assert isinstance(start, UsageAttemptStarted)
    assert isinstance(final, UsageAttemptFinalized)
    assert start.context == final.context
    assert final.performance.clock_domain == "executor"
    assert final.usage.input_tokens.value == 2
    assert final.usage.output_tokens.value == 1
    assert final.context.attribution.identity.provider_payer == "payer@example.com"
    assert final.context.attribution.identity.usage_subject_id == "actor@example.com"
    encoded = json.dumps([x.model_dump(mode="json") for x in sink.offers])
    assert "PRIVATE PROMPT" not in encoded
    assert "PRIVATE OUTPUT" not in encoded
    assert "cognis_usage_observations" not in result.get("backend_metadata", {})
    assert binding.request.capture.failed_events == 0


async def test_multiple_physical_submissions_in_one_rpc_keep_chain_and_no_duplicates():
    sink, binding, context, observer = remote_scope()
    for _ in range(2):

        async def dispatch():
            return {"usage": {"input_tokens": 1, "output_tokens": 0}}

        await observer.binding.call(dispatch, operation="native", parameters={})

    envelope = observer.attach({"result": True})
    ingest_executor(envelope, binding, context)
    ingest_executor(envelope, binding, context)
    assert len(sink.offers) == 4
    starts = [x for x in sink.offers if isinstance(x, UsageAttemptStarted)]
    assert starts[1].context.trace.retry_of_attempt_id == starts[0].context.trace.attempt_id
    assert starts[1].context.trace.attempt_ordinal == starts[0].context.trace.attempt_ordinal + 1
    assert binding.request.ordinal == starts[1].context.trace.attempt_ordinal
    assert binding.request.capture.failed_events == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("controller_owner_id", "different-controller"),
        ("coverage_epoch_id", "different-epoch"),
    ],
)
async def test_executor_cannot_rebind_verified_origin(field, value):
    sink, binding, context, observer = remote_scope()

    async def dispatch():
        return {}

    await observer.binding.call(dispatch, operation="native", parameters={})
    envelope = observer.attach({})
    for event in envelope["backend_metadata"]["cognis_usage_observations"]:
        event["context"][field] = value
    ingest_executor(envelope, binding, context)
    assert sink.offers == []
    assert binding.request.capture.failed_events == 2


@pytest.mark.parametrize("status", [200, 429])
async def test_native_http_status_and_reported_units_are_observed_without_payload(status):
    sink = Sink()
    binding = UsageBinding(request(sink), "provider-test", "tts-model")
    count = 0

    def dispatch(req):
        nonlocal count
        count += 1
        return httpx.Response(
            status,
            headers={"request-id": "upstream-request", "x-character-count": "17"},
            json={"duration": 1.25, "text": "private transcript"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as client:
        response = await observed_http(
            lambda: client.post("https://provider.test/audio", json={"input": "private input"}),
            binding=binding,
            parameters={},
        )
    assert response.status_code == status
    assert count == 1
    assert len(sink.offers) == 2
    final = sink.offers[-1]
    assert isinstance(final, UsageAttemptFinalized)
    assert final.outcome == ("success" if status == 200 else "error")
    extras = {extra.name: extra for extra in final.usage.extras}
    assert extras["billed_characters"].value == 17
    assert extras["billed_characters"].unit == "characters"
    assert extras["input_audio_duration_ms"].value == 1250
    assert extras["input_audio_duration_ms"].unit == "milliseconds"
    assert final.usage.input_tokens.value is None
    serialized = json.dumps([x.model_dump(mode="json") for x in sink.offers])
    assert "private" not in serialized


async def test_executor_buffer_overflow_is_bounded_and_never_blocks_dispatch():
    _, _, _, observer = remote_scope()
    calls = 0

    async def dispatch() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    for _ in range(25):
        await observer.binding.call(dispatch, operation="native", parameters={})
    assert calls == 25
    assert len(observer.events) <= 32
    assert observer.pending_bytes <= 64 * 1024
    assert observer.dropped > 0
