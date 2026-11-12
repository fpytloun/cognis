"""Private SDK compatibility and physical provider-event lifetime invariants."""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import weakref
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
from openai import APIError, AsyncOpenAI

from cognis.models.usage import UsageAttemptFinalized
from cognis.providers.llm import usage_sdk, usage_sdk_openai
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from tests.unit.test_usage_capture_runtime import Sink, request


def binding(sink):
    return UsageBinding(request(sink), "provider-test", "gpt-4o-mini")


def finalized(sink):
    return [event for event in sink.offers if isinstance(event, UsageAttemptFinalized)]


@asynccontextmanager
async def _peer(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.close()
        await server.wait_closed()


def sse(*, tokens: int | None, error: bool = False) -> bytes:
    events = [{"id": "decoded-stream", "choices": [{"delta": {"content": "PRIVATE DELTA"}}]}]
    if tokens is not None:
        events.append(
            {
                "id": "decoded-stream",
                "choices": [],
                "usage": {"prompt_tokens": tokens, "completion_tokens": 3},
            }
        )
    if error:
        events.append(
            {
                "error": {"message": "PRIVATE ERROR", "type": "server_error"},
                "usage": {"prompt_tokens": 9999, "completion_tokens": 9999},
            }
        )
    return (
        b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
        + b"data: [DONE]\n\n"
    )


def test_private_adapter_original_once_and_observer_failure_isolated(monkeypatch):
    import openai._base_client as sdk

    calls = []
    result = object()

    class Client:
        def _process_response_data(self, *, data, cast_to, response):
            calls.append((data, response))
            return result

    def broken(response, data):
        raise RuntimeError("PRIVATE OBSERVER FAILURE")

    monkeypatch.setattr(sdk, "AsyncAPIClient", Client)
    monkeypatch.setattr(usage_sdk_openai, "_installed", None)
    assert usage_sdk_openai.install_openai_decoder(broken)
    assert usage_sdk_openai.install_openai_decoder(broken)
    response = httpx.Response(200)
    data = {"usage": {"prompt_tokens": 2}}
    assert Client()._process_response_data(data=data, cast_to=dict, response=response) is result
    assert calls == [(data, response)]


@pytest.mark.parametrize("unsupported", ["version", "signature"])
def test_unsupported_private_sdk_remains_unmodified(monkeypatch, unsupported):
    import openai._base_client as sdk

    class Client:
        def _process_response_data(self, *, data, cast_to, response):
            return data

    if unsupported == "signature":

        def alternate(self, data):
            return data

        Client._process_response_data = alternate
    else:
        monkeypatch.setattr(usage_sdk_openai, "version", lambda _: "2.30.1")
    monkeypatch.setattr(sdk, "AsyncAPIClient", Client)
    monkeypatch.setattr(usage_sdk_openai, "_installed", None)
    original = Client._process_response_data
    assert not usage_sdk_openai.install_openai_decoder(lambda *_: pytest.fail("observer ran"))
    assert Client._process_response_data is original
    if unsupported == "signature":
        assert Client()._process_response_data({"value": 2}) == {"value": 2}
    else:
        assert Client()._process_response_data(
            data={"value": 2},
            cast_to=dict,
            response=httpx.Response(200),
        ) == {"value": 2}


def test_response_weak_key_is_not_retained_and_late_callbacks_do_not_grow_scope():
    sink = Sink()
    scope = usage_sdk.SDKScope(
        binding(sink),
        parameters={"stream": True},
        operation="chat",
        profile="openai",
    )
    outgoing = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    with scope.activate():
        physical = scope.begin(outgoing.url.path)
        assert physical is not None
        response = httpx.Response(200, request=outgoing)
        scope.bind_response(response, physical)
        ref = weakref.ref(response)
        assert len(scope.responses) == 1
        del response
        gc.collect()
        assert ref() is None
        assert not scope.responses
        scope.finish()
        late = httpx.Response(200, request=outgoing)
        before = len(sink.offers)
        scope.bind_response(late, physical)
        usage_sdk._observe_provider_event(late, {"usage": {"prompt_tokens": 9999}})
        assert not scope.responses and not scope.pending
        assert len(sink.offers) == before


async def test_installed_shared_sdk_interleaves_actors_without_usage_or_scope_leaks(monkeypatch):
    sink = Sink()
    template = request(sink)
    captured_scopes = []
    original_bind = usage_sdk.SDKScope.bind_response

    def track(scope, response, submission):
        original_bind(scope, response, submission)
        if scope not in captured_scopes:
            captured_scopes.append(scope)

    monkeypatch.setattr(usage_sdk.SDKScope, "bind_response", track)
    received = 0
    both = asyncio.Event()

    async def reply(reader, writer):
        nonlocal received
        header = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in header.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        data = json.loads(await reader.readexactly(length))
        tokens = 11 if data["model"] == "model-a" else 23
        received += 1
        if received == 2:
            both.set()
        await both.wait()
        body = sse(tokens=tokens)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        # Exercise the existing SDK decoder across byte/UTF-8/SSE boundaries.
        for part in (body[:13], body[13:77], body[77:]):
            writer.write(part)
            await writer.drain()
            await asyncio.sleep(0)
        writer.close()
        await writer.wait_closed()

    async with (
        _peer(reply) as endpoint,
        AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url=endpoint,
            max_retries=2,
        ) as client,
    ):

        async def run(actor, model):
            identity = template.attribution.identity.model_copy(
                update={
                    "principal_subject": actor,
                    "usage_subject_id": actor,
                }
            )
            logical = template.capture.request(
                attribution=template.attribution.model_copy(
                    update={"identity": identity},
                )
            )
            target = UsageBinding(logical, "provider-test", model)
            stream = await observed_sdk(
                client.chat.completions.create,
                target,
                operation="chat",
            )(model=model, messages=[{"role": "user", "content": "PRIVATE PROMPT"}], stream=True)
            async for _ in stream:
                pass

        await asyncio.gather(run("actor-a@test", "model-a"), run("actor-b@test", "model-b"))
    finals = finalized(sink)
    assert len(finals) == 2 and received == 2
    assert {
        event.context.attribution.identity.usage_subject_id: event.usage.input_tokens.value
        for event in finals
    } == {"actor-a@test": 11, "actor-b@test": 23}
    assert all(event.performance.ttft_ms is not None for event in finals)
    assert all(
        scope.closed and not scope.responses and not scope.pending for scope in captured_scopes
    )
    assert "PRIVATE" not in json.dumps([event.model_dump(mode="json") for event in sink.offers])


@pytest.mark.parametrize("partial_usage", [False, True])
async def test_installed_sdk_partial_error_keeps_only_decoded_provider_usage(partial_usage):
    sink = Sink()

    async def reply(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = sse(tokens=2 if partial_usage else None, error=True)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with (
        _peer(reply) as endpoint,
        AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url=endpoint,
        ) as client,
    ):
        stream = await observed_sdk(
            client.chat.completions.create,
            binding(sink),
            operation="chat",
        )(model="gpt-4o-mini", messages=[], stream=True)
        with pytest.raises(APIError):
            async for _ in stream:
                pass
    final = finalized(sink)[0]
    assert len(finalized(sink)) == 1
    assert final.outcome == "error"
    assert final.usage.input_tokens.value == (2 if partial_usage else None)
    assert final.usage.output_tokens.value == (3 if partial_usage else None)
    assert final.performance.ttft_ms is not None


@pytest.mark.parametrize("observer_failure", [False, True])
async def test_installed_sdk_close_preserves_partial_usage_and_isolates_observer(
    monkeypatch,
    observer_failure,
):
    sink = Sink()
    if observer_failure:

        def broken(*args, **kwargs):
            raise RuntimeError("PRIVATE OBSERVER FAILURE")

        monkeypatch.setattr(usage_sdk.SDKScope, "decoded_response", broken)

    async def reply(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = sse(tokens=2)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with (
        _peer(reply) as endpoint,
        AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url=endpoint,
        ) as client,
    ):
        stream = await observed_sdk(
            client.chat.completions.create,
            binding(sink),
            operation="chat",
        )(model="gpt-4o-mini", messages=[], stream=True)
        await anext(stream)
        await anext(stream)  # Usage arrives before deliberate early closure.
        await stream.aclose()
    final = finalized(sink)[0]
    assert final.outcome == "cancelled"
    assert final.usage.input_tokens.value == (None if observer_failure else 2)
    assert final.usage.output_tokens.value == (None if observer_failure else 3)
    assert len(finalized(sink)) == 1


@pytest.mark.parametrize("unsupported", ["version", "signature"])
async def test_installed_unsupported_decoder_keeps_inference_and_unknown_usage(
    monkeypatch,
    unsupported,
):
    import openai._base_client as sdk

    original = inspect.unwrap(sdk.AsyncAPIClient._process_response_data)
    if unsupported == "signature":

        def alternate(client, **kwargs):
            return original(client, **kwargs)

        monkeypatch.setattr(sdk.AsyncAPIClient, "_process_response_data", alternate)
    else:
        monkeypatch.setattr(sdk.AsyncAPIClient, "_process_response_data", original)
        monkeypatch.setattr(usage_sdk_openai, "version", lambda _: "2.30.1")
    monkeypatch.setattr(usage_sdk_openai, "_installed", None)
    assert not usage_sdk_openai.install_openai_decoder(usage_sdk._observe_provider_event)
    monkeypatch.setattr(usage_sdk, "_OPENAI_DECODER_INSTALLED", False)
    sink = Sink()

    async def reply(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = sse(tokens=2)
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with (
        _peer(reply) as endpoint,
        AsyncOpenAI(api_key="capture-fixture-key", base_url=endpoint) as client,
    ):
        stream = await observed_sdk(
            client.chat.completions.create,
            binding(sink),
            operation="chat",
        )(model="gpt-4o-mini", messages=[], stream=True)
        text = []
        async for chunk in stream:
            if chunk.choices:
                text.append(chunk.choices[0].delta.content)
    assert text == ["PRIVATE DELTA"]
    final = finalized(sink)[0]
    assert final.outcome == "success"
    assert final.usage.input_tokens.value is None
    assert final.usage.output_tokens.value is None
    assert final.usage.input_tokens.status == "unknown"
    assert final.performance.ttft_ms is None


async def test_installed_sdk_task_cancellation_keeps_partial_provider_usage():
    sink = Sink()
    release = asyncio.Event()
    closed = asyncio.Event()
    sends = 0

    async def reply(reader, writer):
        nonlocal sends
        try:
            await reader.readuntil(b"\r\n\r\n")
            sends += 1
            body = sse(tokens=2).removesuffix(b"data: [DONE]\n\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                + f"{len(body):x}\r\n".encode()
                + body
                + b"\r\n"
            )
            await writer.drain()
            await release.wait()
        finally:
            writer.close()
            try:
                with suppress(ConnectionError):
                    await writer.wait_closed()
            finally:
                closed.set()

    try:
        async with (
            asyncio.timeout(10),
            _peer(reply) as endpoint,
        ):
            try:
                async with AsyncOpenAI(
                    api_key="capture-fixture-key",
                    base_url=endpoint,
                    max_retries=2,
                ) as client:
                    stream = await observed_sdk(
                        client.chat.completions.create,
                        binding(sink),
                        operation="chat",
                    )(model="gpt-4o-mini", messages=[], stream=True)
                    await anext(stream)
                    await anext(stream)
                    consuming = asyncio.create_task(anext(stream))
                    await asyncio.sleep(0)
                    consuming.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await consuming
                    await stream.aclose()
            finally:
                # Server.wait_closed() in Python 3.12 waits for accepted sockets.
                release.set()
    finally:
        release.set()
        if sends:
            await asyncio.wait_for(closed.wait(), 3)
    final = finalized(sink)[0]
    assert sends == 1 and len(sink.offers) == 2
    assert final.outcome == "cancelled"
    assert final.usage.input_tokens.value == 2
    assert final.usage.output_tokens.value == 3
    assert final.usage.input_tokens.status == "partial"
    assert final.performance.ttft_ms is not None
