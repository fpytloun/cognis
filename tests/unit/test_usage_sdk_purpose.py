"""Canonical resource grants exclude sibling I/O without changing SDK behavior."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from cognis.models.usage import UsageAttemptFinalized
from cognis.providers.llm import usage_sdk, usage_sdk_dispatch, usage_sdk_purpose
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from tests.unit.test_usage_capture_runtime import Sink, request
from tests.unit.test_usage_factory import capture_provider as capture_provider
from tests.unit.test_usage_sdk_http import completion


async def test_real_sdk_auxiliary_and_custom_authority_auth_are_not_submissions():
    sink = Sink()
    wire = []
    retried = False

    async def peer(outgoing):
        nonlocal retried
        wire.append((outgoing.url.host, outgoing.url.path))
        if outgoing.url.host == "callback.test" or outgoing.url.host == "custom-authority.test":
            return httpx.Response(200, json=completion(input_tokens=900, tokens=901))
        if outgoing.url.host == "inference.test":
            return httpx.Response(
                307,
                headers={"location": "https://redirect.test/v1/chat/completions"},
            )
        if not retried:
            retried = True
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {}})
        return httpx.Response(200, json=completion())

    transport = httpx.MockTransport(peer)
    async with httpx.AsyncClient(transport=transport) as auxiliary:

        async def callback(outgoing):
            await auxiliary.post("https://callback.test/events")
            await auxiliary.post("https://custom-authority.test/session/token")

        async with (
            httpx.AsyncClient(
                transport=transport,
                follow_redirects=True,
                event_hooks={"request": [callback]},
            ) as http,
            AsyncOpenAI(
                api_key="capture-fixture-key",
                base_url="https://inference.test/v1",
                http_client=http,
                max_retries=1,
            ) as client,
        ):
            result = await observed_sdk(
                client.chat.completions.create,
                UsageBinding(request(sink), "provider-test", "gpt-test"),
                operation="chat",
            )(model="gpt-test", messages=[])
    assert result.choices[0].message.content == "private completion"
    assert len(wire) == 12
    finals = [event for event in sink.offers if isinstance(event, UsageAttemptFinalized)]
    assert len(sink.offers) == 8 and len(finals) == 4
    assert [event.outcome for event in finals] == ["unknown", "error", "unknown", "success"]
    assert [event.usage.input_tokens.value for event in finals] == [None, None, None, 2]
    assert [event.usage.output_tokens.value for event in finals] == [None, None, None, 3]
    assert "callback" not in json.dumps([event.model_dump(mode="json") for event in sink.offers])


def test_grant_is_bound_to_the_original_request_operation_and_live_scope():
    sink = Sink()
    first = usage_sdk.SDKScope(
        UsageBinding(request(sink), "provider-test", "gpt-test"),
        parameters={},
        operation="chat",
        profile="openai",
    )
    other = usage_sdk.SDKScope(
        UsageBinding(request(sink), "provider-test", "gpt-test"),
        parameters={},
        operation="chat",
        profile="openai",
    )
    original = httpx.Request("POST", "https://inference.test/v1/chat/completions")
    usage_sdk_purpose.grant_request(first, original, "chat")
    assert usage_sdk._enabled(first, original)
    assert not usage_sdk._enabled(other, original)
    copied = httpx.Request(
        "POST",
        original.url,
        extensions=original.extensions,
    )
    assert not usage_sdk._enabled(first, copied)
    spoofed = httpx.Request(
        "POST",
        original.url,
        extensions={"cognis.inference_submission": "chat"},
    )
    assert not usage_sdk._enabled(first, spoofed)
    first.finish()
    assert not usage_sdk._enabled(first, original)
    other.finish()


async def test_purpose_observer_failure_does_not_fail_or_replay_sdk_inference(monkeypatch):
    sink = Sink()
    sent = []

    async def peer(outgoing):
        sent.append(outgoing.url.path)
        return httpx.Response(200, json=completion())

    def unavailable(*args):
        raise RuntimeError("PRIVATE OBSERVER ERROR")

    monkeypatch.setattr(usage_sdk_dispatch, "grant_request", unavailable)
    logical = request(sink)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(peer)) as http,
        AsyncOpenAI(
            api_key="capture-fixture-key",
            base_url="https://inference.test/v1",
            http_client=http,
            max_retries=1,
        ) as client,
    ):
        result = await observed_sdk(
            client.chat.completions.create,
            UsageBinding(logical, "provider-test", "gpt-test"),
            operation="chat",
        )(model="gpt-test", messages=[])
    assert result.choices[0].message.content == "private completion"
    assert sent == ["/v1/chat/completions"]
    assert sink.offers == []
    assert logical.capture.failed_events >= 1


async def test_non_openai_native_resource_excludes_even_same_client_same_target_callbacks(
    monkeypatch,
):
    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    sink = Sink()
    sent = []
    callback_entered = False
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 2,
            "candidatesTokenCount": 3,
            "totalTokenCount": 5,
        },
    }

    async def peer(outgoing):
        sent.append(outgoing.url)
        return httpx.Response(200, json=payload)

    async def callback(outgoing):
        nonlocal callback_entered
        if callback_entered:
            return
        callback_entered = True
        await http.post("https://custom-authority.test/token")
        await http.post(outgoing.url)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(peer),
        event_hooks={"request": [callback]},
    ) as http:
        monkeypatch.setattr(AsyncHTTPHandler, "create_client", lambda *args, **kwargs: http)
        client = AsyncHTTPHandler()
        result = await observed_sdk(
            litellm.acompletion,
            UsageBinding(request(sink), "provider-test", "gemini/gemini-2.0-flash"),
            operation="chat",
        )(
            model="gemini/gemini-2.0-flash",
            messages=[{"role": "user", "content": "PRIVATE"}],
            api_key="capture-fixture-key",
            api_base="https://inference.test/v1",
            client=client,
        )
    assert result.choices[0].message.content == "ok"
    assert len(sent) == 3
    finals = [event for event in sink.offers if isinstance(event, UsageAttemptFinalized)]
    assert len(sink.offers) == 2 and len(finals) == 1
    assert finals[0].outcome == "success"


@pytest.mark.parametrize("provider", ["gemini", "groq"])
async def test_deferred_native_stream_mints_purpose_when_it_really_submits(monkeypatch, provider):
    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    sink = Sink()
    sent = []
    if provider == "gemini":
        event = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "ok"}]},
                    "finishReason": "STOP",
                }
            ],
        }
        model = "gemini/gemini-2.0-flash"
    else:
        event = {
            "id": "stream-response",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "llama-3.3-70b-versatile",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        model = "groq/llama-3.3-70b-versatile"
    sse = f"data: {json.dumps(event)}\n\n".encode()
    if provider == "groq":
        sse += b"data: [DONE]\n\n"

    async def peer(outgoing):
        sent.append(outgoing.url)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    async with httpx.AsyncClient(transport=httpx.MockTransport(peer)) as http:
        monkeypatch.setattr(AsyncHTTPHandler, "create_client", lambda *args, **kwargs: http)
        client = AsyncHTTPHandler()
        stream = await observed_sdk(
            litellm.acompletion,
            UsageBinding(request(sink), "provider-test", model),
            operation="chat",
        )(
            model=model,
            messages=[{"role": "user", "content": "PRIVATE"}],
            api_key="capture-fixture-key",
            api_base="https://inference.test/v1",
            client=client,
            stream=True,
            num_retries=0,
            max_retries=0,
        )
        chunks = [chunk async for chunk in stream]
        await stream.aclose()
    assert any(chunk.choices[0].delta.content == "ok" for chunk in chunks if chunk.choices)
    assert len(sent) == 1
    finals = [event for event in sink.offers if isinstance(event, UsageAttemptFinalized)]
    assert len(sink.offers) == 2 and len(finals) == 1
    assert finals[0].outcome == "success"


async def test_arbitrary_aiohttp_post_in_an_sdk_scope_has_no_inference_authority(capture_provider):
    import aiohttp

    sink = Sink()
    logical = request(sink)
    url, calls = capture_provider
    async with aiohttp.ClientSession() as session:

        async def auxiliary(**kwargs):
            async with session.post(
                f"{url}/responses",
                json={"input": [{"role": "user", "content": "PRIVATE PROMPT"}]},
            ) as response:
                return await response.json()

        result = await observed_sdk(
            auxiliary,
            UsageBinding(logical, "provider-test", "gpt-4o-mini"),
            operation="responses",
        )(model="gpt-4o-mini")
    assert result["status"] == "completed" and calls == ["/v1/responses"]
    assert sink.offers == []
    assert logical.capture.failed_events >= 1


@pytest.mark.parametrize(
    "family",
    ["groq-chat", "bedrock-converse", "ollama-embeddings", "gemini-embeddings"],
)
async def test_installed_legacy_resource_families_have_positive_purpose(monkeypatch, family):
    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    sink = Sink()
    sent = []
    callback_entered = False
    if family == "bedrock-converse":
        payload = {
            "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 2, "outputTokens": 1, "totalTokens": 3},
            "metrics": {"latencyMs": 1},
        }
    elif family == "ollama-embeddings":
        payload = {"embeddings": [[0.1, 0.2]], "prompt_eval_count": 2}
    elif family == "gemini-embeddings":
        payload = {"embeddings": [{"values": [0.1, 0.2]}]}
    else:
        payload = completion()
        payload["service_tier"] = "on_demand"

    async def peer(outgoing):
        sent.append(outgoing.url)
        return httpx.Response(200, json=payload)

    async def callback(outgoing):
        nonlocal callback_entered
        if callback_entered:
            return
        callback_entered = True
        await http.post("https://custom-authority.test/token")
        await http.post(outgoing.url)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(peer),
        event_hooks={"request": [callback]},
    ) as http:
        monkeypatch.setattr(AsyncHTTPHandler, "create_client", lambda *args, **kwargs: http)
        client = AsyncHTTPHandler()
        monkeypatch.setattr(litellm, "module_level_aclient", client)
        kwargs = {
            "api_base": "https://inference.test/v1",
            "api_key": "capture-fixture-key",
            "client": client,
        }
        if family.endswith("embeddings"):
            model = (
                "ollama/nomic-embed-text"
                if family.startswith("ollama")
                else "gemini/text-embedding-004"
            )
            result = await observed_sdk(
                litellm.aembedding,
                UsageBinding(request(sink), "provider-test", model),
                operation="embeddings",
            )(model=model, input=["PRIVATE EMBEDDING INPUT"], **kwargs)
            assert result.data[0]["embedding"] == [0.1, 0.2]
        else:
            model = (
                "groq/llama-3.3-70b-versatile"
                if family.startswith("groq")
                else "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
            )
            if family.startswith("bedrock"):
                kwargs.update(
                    {
                        "aws_access_key_id": "capture-fixture-key",
                        "aws_secret_access_key": "capture-fixture-secret",
                        "aws_region_name": "us-east-1",
                    }
                )
            result = await observed_sdk(
                litellm.acompletion,
                UsageBinding(request(sink), "provider-test", model),
                operation="chat",
            )(model=model, messages=[{"role": "user", "content": "PRIVATE"}], **kwargs)
            assert result.choices[0].message.content in {"ok", "private completion"}
    assert len(sent) == 3
    finals = [event for event in sink.offers if isinstance(event, UsageAttemptFinalized)]
    assert len(sink.offers) == 2 and len(finals) == 1
    assert finals[0].outcome == "success"
