"""Exercise asynchronous accounting at real SDK and transport boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from cognis.models.usage import (
    UsageAttemptFinalized,
    UsageAttemptStarted,
    UsageAttribution,
    UsageIdentity,
    UsageObservation,
)
from cognis.providers.llm.transport import LiteLLMTransport, ObservedLLMTransport
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from cognis.providers.llm.usage_capture import UsageCapture, UsageRequest
from cognis.providers.llm.usage_normalization import UsageAccumulator, meaningful, safe_error
from cognis.providers.llm.usage_parameters import project_parameters


class Sink:
    """Fixture only: record offers, including intentionally rejected events."""

    def __init__(self, *, accept: bool = True, broken: bool = False) -> None:
        self.offers: list[UsageObservation] = []
        self.accept = accept
        self.broken = broken

    def try_emit(self, observation: UsageObservation) -> bool:
        self.offers.append(observation)
        if self.broken:
            raise RuntimeError("sensitive exception body")
        return self.accept


def request(sink: Sink) -> UsageRequest:
    capture = UsageCapture(
        sink=sink,
        controller_owner_id="controller-test",
        coverage_epoch_id="coverage-test",
    )
    return capture.request(
        attribution=UsageAttribution(
            identity=UsageIdentity(
                principal_subject="actor@example.com",
                principal_type="user",
                usage_subject_kind="user",
                usage_subject_id="actor@example.com",
                provider_payer="payer@example.com",
            ),
            purpose="chat",
        ),
        requested_model="routes/interactive",
        requested_parameters=project_parameters({"temperature": 0.7}),
    )


def selection(req: UsageRequest, *, streaming: bool = False, model: str = "gpt-test"):
    return req.selection(
        provider_id="provider-test",
        model=model,
        operation="chat",
        streaming=streaming,
        wire_parameters={"temperature": 0.5},
    )


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ({"temperature": 0.5}, "codex"),
        ({"service_tier": "priority"}, "priority"),
        ({"service_tier": "secret-tier"}, None),
        ({"service_tier": "auto"}, None),
        ({"speed": "fast"}, None),
    ],
)
def test_omitted_tier_bills_model_default_tier_but_dropped_tier_stays_unknown(wire, expected):
    from cognis.models.config import ModelInfo

    req = request(Sink())
    chosen = req.selection(
        provider_id="provider-test",
        model="gpt-test",
        operation="responses",
        streaming=False,
        wire_parameters=wire,
        model_info=ModelInfo(model_id="gpt-test", tier="codex"),
    )
    assert chosen.service_tier == expected
    assert chosen.effective_parameters.service_tier == (
        wire.get("service_tier") if wire.get("service_tier") in {"priority", "auto"} else None
    )
    assert (
        req.selection(
            provider_id="provider-test",
            model="gpt-test",
            operation="responses",
            streaming=False,
            wire_parameters={"temperature": 0.5},
        ).service_tier
        is None
    )


async def test_systemone_openai_usage_yields_complete_billable_units(monkeypatch):
    from cognis.core.usage.pricing import billable_units
    from cognis.providers.llm import systemone

    sink = Sink()
    req = request(sink)
    binding = UsageBinding(req, "provider-test", "typed-test")

    def handler(outgoing: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {"prompt_tokens": 120, "completion_tokens": 30}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        systemone.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(systemone, "validate_systemone_response", lambda data, payload: data)
    payload = systemone.SystemOneRequest.model_validate(
        {
            "model": "typed-test",
            "state": "x",
            "questions": {"q": {"type": "noul", "instructions": "?"}},
        }
    )
    await systemone.request_systemone(
        payload,
        preset="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        timeout=5,
        usage_binding=binding,
    )
    final = next(o for o in sink.offers if isinstance(o, UsageAttemptFinalized))
    units, complete = billable_units(final.usage, final.context.selection)
    assert units == {"input": 120, "output": 30}
    assert complete


@pytest.mark.parametrize(
    ("extra", "expected"), [({}, "standard"), ({"service_tier": "flex"}, "flex")]
)
async def test_real_sdk_call_without_tier_bills_model_default_tier(extra, expected):
    from cognis.core.usage.pricing import billable_units
    from cognis.models.config import ModelInfo

    sink = Sink()
    req = request(sink)

    def handle(outgoing: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "completion-test",
                "object": "chat.completion",
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        )

    binding = UsageBinding(
        req, "provider-test", "gpt-test", model_info=ModelInfo(model_id="gpt-test")
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        AsyncOpenAI(
            api_key="test-key",
            base_url="https://provider.example/v1",
            http_client=http,
            max_retries=0,
        ) as client,
    ):

        async def dispatch(**kwargs):
            return await client.chat.completions.create(
                model="gpt-test", messages=[{"role": "user", "content": "x"}], **extra
            )

        await observed_sdk(dispatch, binding, operation="chat")(model="gpt-test", **extra)
    final = next(o for o in sink.offers if isinstance(o, UsageAttemptFinalized))
    assert final.context.selection.service_tier == expected
    units, complete = billable_units(final.usage, final.context.selection)
    assert units == {"input": 8, "output": 4} and complete


async def test_embedding_usage_without_output_counter_is_complete():
    from cognis.core.usage.pricing import billable_units

    sink = Sink()
    req = request(sink)
    selection = req.selection(
        provider_id="provider-test",
        model="embed-test",
        operation="embeddings",
        streaming=False,
        wire_parameters={"input": ["x"]},
    )

    async def dispatch():
        return {"usage": {"prompt_tokens": 13, "total_tokens": 13}}

    await req.call(dispatch, selection=selection, semantic_profile="openai")
    final = next(o for o in sink.offers if isinstance(o, UsageAttemptFinalized))
    assert final.usage.completeness == "reported"
    units, complete = billable_units(final.usage, final.context.selection)
    assert units == {"input": 13} and complete


def test_tierless_resource_without_declared_tariff_bills_default_tier():
    from cognis.models.config import ModelInfo
    from cognis.models.usage import UsageServiceTierApplicability

    req = request(Sink())
    evidence = UsageServiceTierApplicability.model_validate(
        {"state": "not_applicable", "scope": {"resource": "openai.embeddings.create"}}
    )
    chosen = req.selection(
        provider_id="provider-test",
        model="embed-test",
        operation="embeddings",
        streaming=False,
        wire_parameters={"input": ["x"]},
        model_info=ModelInfo(model_id="embed-test"),
        applicability=evidence,
    )
    assert chosen.service_tier == "standard"
    assert chosen.service_tier_applicability is None


def test_openai_usage_without_cache_write_counter_keeps_fresh_input_billable():
    from cognis.models.usage_normalization import normalize_usage_tokens
    from cognis.providers.llm.usage_normalization import UsageAccumulator

    usage = UsageAccumulator("openai")
    usage.observe(
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 10,
                "input_tokens_details": {"cached_tokens": 900},
            }
        }
    )
    billable = normalize_usage_tokens(usage.normalized(partial=False)).billable
    assert billable.input.value == 100
    assert billable.cache_read.value == 900
    assert billable.cache_write.status == "not_applicable"


@pytest.mark.parametrize("accept,broken", [(True, False), (False, False), (True, True)])
async def test_sink_failure_and_overflow_never_gate_dispatch_or_replay(accept, broken):
    sink = Sink(accept=accept, broken=broken)
    req = request(sink)
    calls = 0
    response = {"usage": {"prompt_tokens": 0, "completion_tokens": 2}}

    async def dispatch():
        nonlocal calls
        calls += 1
        assert len(sink.offers) == 1
        assert isinstance(sink.offers[0], UsageAttemptStarted)
        return response

    result = await req.call(dispatch, selection=selection(req), semantic_profile="openai")
    assert result is response
    assert calls == 1
    assert len(sink.offers) == 2
    start, final = sink.offers
    assert isinstance(final, UsageAttemptFinalized)
    assert start.context == final.context
    assert final.context.started_at == start.context.started_at
    assert final.usage.input_tokens.value == 0
    assert final.usage.input_tokens.status == "reported"
    assert final.performance.ttft_ms is None
    assert final.context.attribution.identity.usage_subject_id == "actor@example.com"
    assert final.context.attribution.identity.provider_payer == "payer@example.com"
    assert req.capture.failed_events == (2 if broken else 0)
    assert req.capture.rejected_events == (2 if not accept else 0)


async def test_cancellation_keeps_partial_counts_and_closes_stream_once():
    sink = Sink()
    req = request(sink)
    entered = asyncio.Event()
    closed = 0

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        nonlocal closed
        try:
            yield {"choices": [{"delta": {"role": "assistant"}}]}
            yield {
                "choices": [{"delta": {"content": "text"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed += 1

    async def dispatch():
        return chunks()

    stream = await req.call(dispatch, selection=selection(req, streaming=True))

    async def consume():
        async with aclosing(stream):
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == 1
    assert len(sink.offers) == 2
    final = sink.offers[-1]
    assert isinstance(final, UsageAttemptFinalized)
    assert final.outcome == "cancelled"
    assert final.usage.output_tokens.value == 3
    assert final.usage.output_tokens.status == "partial"
    assert final.performance.ttft_ms is not None
    assert final.error.summary_code == "request_cancelled"


async def test_close_before_first_chunk_records_cancelled_unknown_usage():
    sink = Sink()
    req = request(sink)

    async def chunks():
        yield {"choices": [{"delta": {"content": "not consumed"}}]}

    async def dispatch():
        return chunks()

    stream = await req.call(dispatch, selection=selection(req, streaming=True))
    await stream.aclose()
    await stream.aclose()
    assert len(sink.offers) == 2
    assert sink.offers[-1].outcome == "cancelled"
    assert sink.offers[-1].usage.input_tokens.value is None


async def test_configured_sdk_retry_has_distinct_physical_attempts():
    sink = Sink()
    req = request(sink)
    network = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal network
        network += 1
        assert len(sink.offers) == (1 if network == 1 else 3)
        if network == 1:
            return httpx.Response(503, json={"error": {"message": "secret prompt"}})
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-test"},
            json={
                "id": "completion-test",
                "object": "chat.completion",
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "secret output"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        AsyncOpenAI(
            api_key="test-key",
            base_url="https://provider.example/v1",
            http_client=http,
            max_retries=1,
        ) as client,
    ):

        async def dispatch(**kwargs):
            return await client.chat.completions.create(
                model="gpt-test",
                messages=[{"role": "user", "content": "secret prompt"}],
            )

        await observed_sdk(
            dispatch, UsageBinding(req, "provider-test", "gpt-test"), operation="chat"
        )(model="gpt-test")
        assert client.max_retries == 1
    assert network == 2
    first, second = sink.offers[1], sink.offers[3]
    assert len(sink.offers) == 4
    assert first.outcome == "error"
    assert first.usage.input_tokens.value is None
    assert first.usage.output_tokens.value is None
    assert second.usage.input_tokens.value == 8
    assert second.usage.output_tokens.value == 4
    assert second.context.trace.retry_of_attempt_id == first.context.trace.attempt_id
    assert second.context.trace.attempt_id != first.context.trace.attempt_id
    assert second.context.trace.request_id == first.context.trace.request_id
    assert second.provider_request_id == "provider-request-test"
    encoded = "".join(item.model_dump_json() for item in sink.offers)
    assert "secret" not in encoded
    assert "test-key" not in encoded


async def test_fallback_has_distinct_target_and_attempt_trail():
    sink = Sink()
    req = request(sink)

    async def dispatch():
        return {}

    await req.call(dispatch, selection=selection(req))
    await req.call(dispatch, selection=selection(req, model="gpt-fallback"))
    a, b = sink.offers[1], sink.offers[3]
    assert b.context.trace.fallback_from_attempt_id == a.context.trace.attempt_id
    assert b.context.trace.retry_of_attempt_id is None
    assert b.context.trace.attempt_ordinal == 2


async def test_real_sdk_stream_close_releases_http_response_and_emits_no_chunk_events():
    sink = Sink()
    req = request(sink)
    closed = 0

    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"stream-test","object":"chat.completion.chunk","model":"gpt-test","choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
            yield b'data: {"id":"stream-test","object":"chat.completion.chunk","model":"gpt-test","choices":[{"index":0,"delta":{"content":"text"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            nonlocal closed
            closed += 1

    def handle(_: httpx.Request) -> httpx.Response:
        assert len(sink.offers) == 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes())

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        AsyncOpenAI(
            api_key="test-key",
            base_url="https://provider.example/v1",
            http_client=http,
            max_retries=0,
        ) as client,
    ):

        async def dispatch(**kwargs):
            return await client.chat.completions.create(
                model="gpt-test",
                messages=[{"role": "user", "content": "text"}],
                stream=True,
            )

        stream = await observed_sdk(
            dispatch, UsageBinding(req, "provider-test", "gpt-test"), operation="chat"
        )(model="gpt-test", stream=True)
        await anext(stream)
        assert req.first_generated_monotonic is None
        await anext(stream)
        assert req.first_generated_monotonic is not None
        assert len(sink.offers) == 1
        await stream.close()
        await stream.aclose()
        assert closed == 1
        assert len(sink.offers) == 2
        assert sink.offers[-1].outcome == "cancelled"


async def test_transport_dependency_strips_controller_metadata_and_preserves_sdk_retries(
    monkeypatch,
):
    import litellm

    sink = Sink()
    req = request(sink)
    received = []

    async def completion_sdk(**kwargs):
        received.append(kwargs)
        return await client.chat.completions.create(
            model=kwargs["model"],
            messages=[{"role": "user", "content": "text"}],
            temperature=kwargs["temperature"],
        )

    async def responses_sdk(**kwargs):
        received.append(kwargs)
        return await client.responses.create(
            model=kwargs["model"],
            input="text",
            max_output_tokens=kwargs["max_output_tokens"],
        )

    def handle(http_request):
        assert "cognis_" not in http_request.content.decode()
        if http_request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "id": "response-test",
                    "object": "response",
                    "created_at": 0,
                    "model": "gpt-test",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "completion-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-test",
                "choices": [],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        )

    monkeypatch.setattr(litellm, "acompletion", completion_sdk)
    monkeypatch.setattr(litellm, "aresponses", responses_sdk)
    transport = ObservedLLMTransport(
        LiteLLMTransport(),
        usage_request=req,
        provider_id="provider-test",
        effective_model="gpt-test",
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        AsyncOpenAI(api_key="test-key", http_client=http, max_retries=4) as client,
    ):
        await transport.completion(
            model="gpt-test",
            stream=False,
            temperature=0.2,
            cognis_session_id="session-test",
            num_retries=3,
            max_retries=4,
        )
        await transport.responses(
            model="gpt-test",
            stream=False,
            max_output_tokens=123,
            num_retries=3,
            max_retries=4,
        )
        assert client.max_retries == 4
    assert len(received) == 2
    assert all(call["num_retries"] == 3 and call["max_retries"] == 4 for call in received)
    assert "cognis_session_id" not in received[0]
    assert sink.offers[1].context.selection.effective_parameters.temperature == 0.2
    assert sink.offers[3].context.selection.effective_parameters.max_output_tokens == 123


async def test_schema_incompatible_model_is_counted_gap_not_inference_failure():
    sink = Sink()
    req = request(sink)

    class Transport:
        async def completion(self, **kwargs):
            return kwargs["model"]

        responses = completion

    model = "x" * 513
    from cognis.providers.llm.usage_binding import UsageBinding

    binding = UsageBinding(req, "provider-test", model)
    assert (
        await binding.call(
            lambda: Transport().completion(model=model),
            operation="chat",
            parameters={},
        )
        == model
    )
    assert req.capture.failed_events == 1
    assert sink.offers == []


@pytest.mark.parametrize("length", [180, 512])
async def test_printable_long_model_names_are_captured_without_loss(length):
    from cognis.providers.llm.usage_binding import UsageBinding

    sink = Sink()
    model = "č" * length
    req = request(sink)
    req = req.capture.request(attribution=req.attribution, requested_model=model)

    class Transport:
        async def completion(self, **kwargs):
            return {"model": kwargs["model"], "usage": {"prompt_tokens": 1, "completion_tokens": 0}}

        responses = completion

    result = await UsageBinding(req, "provider-test", model).call(
        lambda: Transport().completion(model=model),
        operation="chat",
        parameters={},
    )
    assert result["model"] == model
    assert len(sink.offers) == 2
    assert req.capture.failed_events == req.capture.rejected_events == 0
    for event in sink.offers:
        assert event.context.selection.effective_model == model
        assert event.context.selection.requested_model == model
    assert sink.offers[-1].provider_reported_model == model


def test_dense_context_retains_resources_unicode_and_all_parameter_provenance():
    from typing import get_args

    from cognis.models.usage import UsageParameterName
    from tests.usage_rich_fixture import dense_usage_event

    original = dense_usage_event()
    sink = Sink()
    req = request(sink)
    fitted = req._fit_context(original.context)
    assert fitted == original.context
    assert fitted.metadata_reduction is None
    assert req.capture.reduced_contexts == 0
    assert fitted.attribution.resources is not None
    assert fitted.attribution.hook.hook_name == original.context.attribution.hook.hook_name
    for name in get_args(UsageParameterName):
        assert getattr(fitted.selection.requested_parameters, name) == getattr(
            original.context.selection.requested_parameters, name
        )
        assert fitted.selection.requested_parameters.provenance_source(
            name
        ) == original.context.selection.requested_parameters.provenance_source(name)


def test_parameters_never_include_payloads_or_unbounded_provider_strings():
    value = project_parameters(
        {
            "temperature": True,
            "top_p": float("nan"),
            "top_k": 10**500,
            "max_tokens": 32,
            "seed": -2,
            "response_format": {"type": "json_schema", "json_schema": {"secret": "payload"}},
            "tool_choice": {"type": "function", "function": {"name": "secret tool"}},
            "reasoning_effort": "high",
            "service_tier": "secret-tier",
            "stop": ["secret stop"],
            "messages": ["secret prompt"],
        }
    )
    assert value.temperature is None
    assert value.top_p is None
    assert value.top_k is None
    assert value.max_output_tokens == 32
    assert value.seed == -2
    assert value.response_format == "json_schema"
    assert value.tool_choice == "named"
    assert value.reasoning_effort == "high"
    assert value.service_tier is None
    assert "secret" not in value.model_dump_json()


def test_usage_snapshot_not_added_repeatedly_and_partial_native_semantics():
    usage = UsageAccumulator("anthropic")
    usage.observe(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 5,
                }
            },
        }
    )
    usage.observe({"type": "message_delta", "usage": {"output_tokens": 2}})
    usage.observe({"type": "message_delta", "usage": {"output_tokens": 4}})
    normalized = usage.normalized(partial=True)
    assert normalized.input_tokens.value == 10
    assert normalized.output_tokens.value == 4
    assert normalized.cache_read_tokens.value == 5
    assert normalized.cache_read_semantics == "additional_to_input"
    assert normalized.provider_total_tokens.value is None
    assert normalized.output_tokens.status == "partial"


@pytest.mark.parametrize(
    "chunk,expected",
    [
        ({"choices": [{"delta": {"role": "assistant"}}]}, False),
        ({"usage": {"completion_tokens": 1}}, False),
        ({"type": "response.created"}, False),
        ({"type": "response.output_text.delta", "delta": "x"}, True),
        ({"type": "content_block_delta", "delta": {"thinking": "x"}}, True),
        ({"choices": [{"delta": {"tool_calls": [{"function": {"name": "lookup"}}]}}]}, True),
    ],
)
def test_meaningful_first_response(chunk, expected):
    assert meaningful(chunk) is expected


def test_error_projection_never_reads_message():
    class UnsafeError(Exception):
        status_code = 429

        def __str__(self):
            raise AssertionError("must not inspect")

    error = safe_error(UnsafeError())
    assert error.summary_code == "provider_rate_limited"
    assert error.http_status == 429
    assert error.provider_error_code is None


async def test_failed_responses_event_is_error_without_raw_provider_message():
    sink = Sink()
    req = request(sink)

    async def dispatch():
        return {
            "type": "response.failed",
            "response": {"status": "failed", "error": {"message": "secret content"}},
        }

    await req.call(dispatch, selection=selection(req))
    final = sink.offers[-1]
    assert final.outcome == "error"
    assert final.error.summary_code == "provider_error"
    assert "secret" not in final.model_dump_json()


def test_async_or_missing_sink_is_a_startup_configuration_error():
    class AsyncSink:
        async def try_emit(self, observation):
            return True

    for sink in (None, AsyncSink()):
        with pytest.raises(TypeError, match="configured synchronous"):
            UsageCapture(
                sink=sink,
                controller_owner_id="controller-test",
                coverage_epoch_id="coverage-test",
            )


async def test_normal_full_parameter_context_fits_terminal_envelope():
    sink = Sink()
    req = request(sink)
    params = project_parameters(
        {
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 1024,
            "reasoning_effort": "high",
            "service_tier": "priority",
            "seed": 17,
            "presence_penalty": 0.1,
            "frequency_penalty": 0.2,
        }
    )
    req.requested_parameters = params

    async def dispatch():
        return {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
                "prompt_tokens_details": {"cached_tokens": 500},
                "completion_tokens_details": {"reasoning_tokens": 50},
            }
        }

    target = selection(req).model_copy(update={"effective_parameters": params})
    await req.call(dispatch, selection=target)
    assert len(sink.offers) == 2
    assert req.capture.failed_events == 0
    assert all(
        len(item.model_dump_json(exclude_none=True, exclude_defaults=True).encode()) <= 4096
        for item in sink.offers
    )
