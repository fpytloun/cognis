"""SDK retry qualification at the actual HTTPX submission boundary."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from openai import APIStatusError, AsyncOpenAI

from cognis.models.usage import UsageAttemptFinalized, UsageIdentity
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from tests.unit.test_usage_capture_runtime import Sink, request
from tests.unit.test_usage_factory import capture_provider as capture_provider


def completion(*, text="private completion", tokens=3, input_tokens=2):
    return {
        "id": "chatcmpl-capture-http",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": tokens,
            "total_tokens": input_tokens + tokens,
        },
    }


async def _chat(http, *, model="gpt-test", stream=False, api_base="https://inference.test/v1"):
    """The installed canonical SDK resource, with HTTP ownership kept by the test."""
    client = AsyncOpenAI(
        api_key="capture-fixture-key",
        base_url=api_base,
        http_client=http,
        max_retries=0,
    )
    return await client.chat.completions.create(model=model, messages=[], stream=stream)


async def test_openai_internal_retry_is_captured_without_suppressing_it():
    sink = Sink()
    scope = request(sink)
    binding = UsageBinding(scope, "provider-test", "gpt-test")
    sent = []

    async def handler(req):
        sent.append(req)
        if len(sent) == 1:
            return httpx.Response(
                429,
                headers={"retry-after-ms": "0"},
                json={"error": {"message": "private upstream error", "type": "rate_limit"}},
            )
        return httpx.Response(200, json=completion(), headers={"x-request-id": "http-success"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url="https://inference.test/v1",
            http_client=http,
            max_retries=1,
        ) as client,
    ):
        result = await observed_sdk(
            client.chat.completions.create,
            binding,
            operation="chat",
        )(model="gpt-test", messages=[{"role": "user", "content": "private prompt"}])
    assert result.choices[0].message.content == "private completion"
    assert len(sent) == 2
    assert len(sink.offers) == 4
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert [item.outcome for item in finals] == ["error", "success"]
    assert finals[0].error.http_status == 429
    assert finals[1].context.trace.retry_of_attempt_id == finals[0].context.trace.attempt_id
    assert finals[1].usage.input_tokens.value == 2
    assert finals[1].usage.output_tokens.value == 3
    assert finals[1].provider_request_id == "http-success"
    assert scope.capture.failed_events == 0
    encoded = json.dumps([item.model_dump(mode="json") for item in sink.offers])
    assert "private prompt" not in encoded
    assert "private completion" not in encoded
    assert "private upstream error" not in encoded


async def test_sdk_replacement_clients_are_observed_and_retry_arguments_are_unchanged():
    sink = Sink()
    scope = request(sink)
    binding = UsageBinding(scope, "provider-test", "gpt-test")
    sent = []
    sdk_kwargs = []

    async def handler(req):
        sent.append(req)
        return httpx.Response(503 if len(sent) == 1 else 200, json=completion())

    async def sdk(**kwargs):
        sdk_kwargs.append(kwargs)
        for _ in range(2):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                try:
                    return (await _chat(client, model=kwargs["model"])).model_dump()
                except APIStatusError as error:
                    assert error.status_code == 503
        raise AssertionError("fixture did not recover")

    result = await observed_sdk(sdk, binding, operation="chat")(
        model="gpt-test",
        num_retries=2,
        max_retries=7,
    )
    assert result["usage"]["completion_tokens"] == 3
    assert sdk_kwargs == [{"model": "gpt-test", "num_retries": 2, "max_retries": 7}]
    assert len(sent) == 2
    assert [item.outcome for item in sink.offers if isinstance(item, UsageAttemptFinalized)] == [
        "error",
        "success",
    ]


async def test_concurrent_sdk_requests_do_not_exchange_subject_or_model():
    sink = Sink()
    base = request(sink)

    async def handler(req):
        await asyncio.sleep(0)
        return httpx.Response(200, json=completion())

    async def run(subject, model):
        attrs = base.attribution.model_copy(
            update={
                "identity": UsageIdentity(
                    principal_type="user",
                    principal_subject=subject,
                    usage_subject_kind="user",
                    usage_subject_id=subject,
                )
            }
        )
        logical = base.capture.request(attribution=attrs, requested_model=model)
        binding = UsageBinding(logical, "provider-test", model)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            async def sdk(**kwargs):
                return await _chat(client, model=kwargs["model"])

            return await observed_sdk(sdk, binding, operation="chat")(model=model)

    await asyncio.gather(
        run("first@example.com", "model-first"),
        run("second@example.com", "model-second"),
    )
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(finals) == 2
    assert {
        (
            item.context.attribution.identity.principal_subject,
            item.context.selection.effective_model,
        )
        for item in finals
    } == {
        ("first@example.com", "model-first"),
        ("second@example.com", "model-second"),
    }
    assert len({item.context.trace.request_id for item in finals}) == 2


async def test_lazy_sdk_stream_retries_keep_scope_and_close_finalizes_once():
    sink = Sink()
    scope = request(sink)
    binding = UsageBinding(scope, "provider-test", "gpt-test")
    sent = []

    async def handler(req):
        sent.append(req)
        if len(sent) == 1:
            return httpx.Response(503, json={"error": {}})
        chunk = completion()
        chunk["object"] = "chat.completion.chunk"
        chunk["choices"][0]["delta"] = chunk["choices"][0].pop("message")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
        )

    async def sdk(**kwargs):
        async def stream():
            for _ in range(2):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    try:
                        response = await _chat(client, stream=True)
                        async with response:
                            async for chunk in response:
                                yield chunk
                        return
                    except APIStatusError as error:
                        assert error.status_code == 503

        return stream()

    stream = await observed_sdk(sdk, binding, operation="chat")(model="gpt-test", stream=True)
    assert sink.offers == []
    chunks = [chunk async for chunk in stream]
    await stream.aclose()
    assert len(chunks) == 1
    assert len(sent) == 2
    assert len(sink.offers) == 4
    assert sink.offers[-1].outcome == "success"
    assert sink.offers[-1].usage.output_tokens.value == 3


async def test_pre_dispatch_sdk_error_does_not_invent_a_physical_attempt():
    sink = Sink()
    scope = request(sink)

    async def sdk(**kwargs):
        raise ValueError("private SDK configuration error")

    with pytest.raises(ValueError, match="private SDK configuration error"):
        await observed_sdk(sdk, UsageBinding(scope, "provider-test", "gpt-test"), operation="chat")(
            model="gpt-test"
        )
    assert sink.offers == []


async def test_large_decoded_response_reports_usage_without_retaining_a_raw_body():
    sink = Sink()
    scope = request(sink)
    text = "sensitive body " * 10_000

    async def handler(req):
        return httpx.Response(200, json=completion(text=text))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url="https://inference.test/v1",
            http_client=http,
            max_retries=0,
        ) as client,
    ):
        await observed_sdk(
            client.chat.completions.create,
            UsageBinding(scope, "provider-test", "gpt-test"),
            operation="chat",
        )(model="gpt-test", messages=[])
    assert len(sink.offers) == 2
    assert sink.offers[-1].usage.output_tokens.value == 3
    assert "sensitive body" not in sink.offers[-1].model_dump_json()


@pytest.mark.parametrize(
    "via_httpx", [False, True], ids=["litellm-native-resource", "openai-aiohttp-bridge"]
)
async def test_installed_aiohttp_paths_have_one_physical_ledger(capture_provider, via_httpx):
    import aiohttp
    import litellm
    from litellm.llms.custom_httpx.aiohttp_transport import LiteLLMAiohttpTransport

    sink = Sink()
    logical = request(sink)
    binding = UsageBinding(logical, "provider-test", "gpt-4o-mini")
    url, calls = capture_provider
    payload = {"input": [{"role": "user", "content": "PRIVATE PROMPT"}]}
    async with aiohttp.ClientSession() as session:
        if via_httpx:
            async with httpx.AsyncClient(
                transport=LiteLLMAiohttpTransport(session, owns_session=False),
            ) as client:
                sdk = AsyncOpenAI(
                    api_key="capture-fixture-key",
                    base_url=url,
                    http_client=client,
                    max_retries=0,
                )
                result = await observed_sdk(
                    sdk.responses.create,
                    binding,
                    operation="responses",
                )(model="gpt-4o-mini", **payload)
        else:
            result = await observed_sdk(
                litellm.aresponses,
                binding,
                operation="responses",
            )(
                model="openai/gpt-4o-mini",
                api_base=url,
                api_key="capture-fixture-key",
                shared_session=session,
                **payload,
            )
    assert result.status == "completed"
    assert calls == ["/v1/responses"]
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(sink.offers) == 2 and len(finals) == 1
    assert finals[0].outcome == "success"
    assert finals[0].usage.input_tokens.value == 2
    assert finals[0].usage.output_tokens.value == 1
    assert logical.capture.failed_events == 0


async def test_finished_sdk_scope_cannot_attribute_inherited_background_http():
    sink = Sink()
    logical = request(sink)
    release = asyncio.Event()
    sent = []
    children = []

    async def handler(req):
        sent.append(req.url.path)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def sdk(**kwargs):
            async def background():
                await release.wait()
                await client.post("https://inference.test/telemetry")

            children.append(asyncio.create_task(background()))
            return await _chat(client)

        await observed_sdk(
            sdk,
            UsageBinding(logical, "provider-test", "gpt-test"),
            operation="chat",
        )(model="gpt-test")
        assert len(sink.offers) == 2
        release.set()
        await asyncio.gather(*children)
    assert len(sent) == 2
    assert len(sink.offers) == 2


async def test_parallel_response_decoding_cannot_duplicate_aggregate_sdk_usage():
    sink = Sink()
    logical = request(sink)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json=completion(
                    input_tokens=2 if req.url.path.startswith("/first/") else 5,
                    tokens=3 if req.url.path.startswith("/first/") else 7,
                ),
            ),
        )
    ) as client:

        async def sdk(**kwargs):
            await asyncio.gather(
                _chat(client, api_base="https://inference.test/first"),
                _chat(client, api_base="https://inference.test/second"),
            )
            return completion(input_tokens=1000, tokens=2000)

        await observed_sdk(
            sdk,
            UsageBinding(logical, "provider-test", "gpt-test"),
            operation="chat",
        )(model="gpt-test")
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(sink.offers) == 4
    assert sorted((x.usage.input_tokens.value, x.usage.output_tokens.value) for x in finals) == [
        (2, 3),
        (5, 7),
    ]


async def test_installed_aiohttp_retry_middleware_is_not_bypassed(capture_provider):
    import aiohttp
    import litellm

    sink = Sink()
    logical = request(sink)
    url, calls = capture_provider
    payload = {"input": [{"role": "user", "content": "PRIVATE PROMPT"}]}

    async def configured_retry(req, handler):
        first = await handler(req)
        await first.json()
        first.release()
        return await handler(req)

    async with aiohttp.ClientSession(middlewares=(configured_retry,)) as client:
        await observed_sdk(
            litellm.aresponses,
            UsageBinding(logical, "provider-test", "gpt-4o-mini"),
            operation="responses",
        )(
            model="openai/gpt-4o-mini",
            api_base=url,
            api_key="capture-fixture-key",
            shared_session=client,
            **payload,
        )
    assert calls == ["/v1/responses", "/v1/responses"]
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(sink.offers) == 4
    assert [x.usage.input_tokens.value for x in finals] == [2, 2]


async def test_installed_litellm_replaces_a_closed_aiohttp_session(capture_provider):
    import aiohttp
    from litellm.llms.custom_httpx.aiohttp_transport import LiteLLMAiohttpTransport

    sink = Sink()
    logical = request(sink)
    url, calls = capture_provider
    closed = aiohttp.ClientSession()
    await closed.close()
    transport = LiteLLMAiohttpTransport(closed)
    async with httpx.AsyncClient(transport=transport) as client:
        sdk = AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url=url,
            http_client=client,
            max_retries=0,
        )
        await observed_sdk(
            sdk.responses.create,
            UsageBinding(logical, "provider-test", "gpt-4o-mini"),
            operation="responses",
        )(model="gpt-4o-mini", input=[{"role": "user", "content": "PRIVATE PROMPT"}])
        assert transport.client is not closed
    assert calls == ["/v1/responses"]
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(sink.offers) == 2
    assert finals[0].usage.input_tokens.value == 2


@pytest.mark.parametrize("reports_usage", [True, False])
async def test_installed_litellm_stream_retry_keeps_physical_usage_and_ttft_separate(reports_usage):
    import json

    import litellm

    sink = Sink()
    logical = request(sink)
    calls = []
    sse = (
        b'data: {"id":"stream-response","object":"chat.completion.chunk","created":0,'
        b'"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"x"}}]}\n\n'
    )
    if reports_usage:
        sse += (
            b'data: {"id":"stream-response","object":"chat.completion.chunk","created":0,'
            b'"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":2,'
            b'"completion_tokens":3,"total_tokens":5}}\n\n'
        )
    sse += b"data: [DONE]\n\n"

    async def peer(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(row.split(b":", 1)[1])
                for row in head.split(b"\r\n")
                if row.lower().startswith(b"content-length:")
            )
            payload = json.loads(await reader.readexactly(length))
            assert payload["messages"][0]["content"] == "PRIVATE STREAM"
            calls.append(head.split(b"\r\n")[0])
            if len(calls) == 1:
                status, content_type = b"503 Unavailable", b"application/json"
                body = b'{"error":{"message":"private upstream failure"}}'
            else:
                status, content_type, body = b"200 OK", b"text/event-stream", sse
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: "
                + content_type
                + b"\r\nRetry-After: 0\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        stream = await observed_sdk(
            litellm.acompletion,
            UsageBinding(logical, "provider-test", "gpt-4o-mini"),
            operation="chat",
        )(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "PRIVATE STREAM"}],
            api_base=f"http://127.0.0.1:{port}/v1",
            api_key="test-key",
            stream=True,
            stream_options={"include_usage": True},
            num_retries=1,
            max_retries=1,
        )
        chunks = [chunk async for chunk in stream]
        await stream.aclose()
    finally:
        server.close()
        await server.wait_closed()
    assert chunks
    assert len(calls) == 2
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(sink.offers) == 4
    assert finals[0].outcome == "error"
    assert finals[0].usage.input_tokens.value is None
    assert finals[0].performance.ttft_ms is None
    assert finals[1].outcome == "success"
    assert finals[1].usage.input_tokens.value == (2 if reports_usage else None)
    assert finals[1].usage.output_tokens.value == (3 if reports_usage else None)
    assert finals[1].performance.ttft_ms is not None
    assert "PRIVATE STREAM" not in json.dumps(
        [item.model_dump(mode="json") for item in sink.offers]
    )
