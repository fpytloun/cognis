"""Media evidence crosses physical transports and executor wire boundaries."""

import json
from contextlib import aclosing

import httpx
import pytest
from openai import AsyncOpenAI

from cognis.executor.usage_observer import executor_observer
from cognis.models.usage import UsageAttemptFinalized
from cognis.providers.llm import speech_streaming
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from cognis.providers.llm.usage_executor import executor_metadata, ingest_executor
from cognis.providers.llm.usage_http import observed_http
from tests.unit.test_speech_streaming import Socket
from tests.unit.test_usage_capture_runtime import Sink, request


async def test_sdk_image_counts_actual_outputs_and_modality_tokens():
    sink = Sink()
    binding = UsageBinding(request(sink), "provider", "gpt-image-1", media_operation="image_edit")

    async def respond(req):
        return httpx.Response(
            200,
            json={
                "created": 1,
                "data": [{"b64_json": "private-image"}, {"b64_json": "private-image"}],
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 400,
                    "total_tokens": 700,
                    "input_tokens_details": {"text_tokens": 100, "image_tokens": 200},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AsyncOpenAI(
            api_key="test-key", base_url="https://provider.test/v1", http_client=http
        )
        result = await observed_sdk(client.images.generate, binding, operation="native")(
            model="gpt-image-1",
            prompt="private prompt",
            n=3,
        )
    assert len(result.data) == 2
    final = sink.offers[-1]
    assert isinstance(final, UsageAttemptFinalized)
    assert final.context.selection.operation == "image_edit"
    assert {x.name: x.value for x in final.usage.extras} == {
        "output_image_count": 2,
        "input_text_tokens": 100,
        "input_image_tokens": 200,
        "output_image_tokens": 400,
    }
    assert "private" not in json.dumps(final.model_dump(mode="json"))


@pytest.mark.parametrize("close_early", [False, True])
async def test_stream_tts_records_characters_and_closure(monkeypatch, close_early):
    sink = Sink()
    binding = UsageBinding(request(sink), "provider", "tts-1", media_operation="synthesize")

    async def respond(req):
        return httpx.Response(200, content=b"\x01\x00" * 24000)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(speech_streaming.httpx, "AsyncClient", lambda **kwargs: client)
    stream = speech_streaming.stream_synthesis(
        text="Spoken text",
        model="tts-1",
        voice="alloy",
        provider_preset="openai",
        request_kwargs={"api_key": "test-key"},
        usage_binding=binding,
    )
    async with aclosing(stream):
        if close_early:
            await anext(stream)
        else:
            events = [event async for event in stream]
            assert events[0].type == "start" and events[-1].type == "done"
            assert events[-1].duration_seconds == 1
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    assert len(finals) == 1
    final = finals[0]
    assert final.context.selection.operation == "synthesize"
    assert {item.name: item.value for item in final.usage.extras}["input_characters"] == 11
    if not close_early:
        assert final.outcome == "success"
        duration = next(x for x in final.usage.extras if x.name == "output_audio_duration_ms")
        assert (duration.value, duration.status) == (1000, "derived")
    else:
        assert final.outcome != "success"
        assert final.usage.completeness == "partial"


async def test_stream_stt_measures_sent_pcm_and_executor_round_trip(monkeypatch):
    sink = Sink()
    binding = UsageBinding(
        request(sink), "provider", "scribe_v2_realtime", media_operation="transcribe"
    )
    metadata, expected = executor_metadata(
        binding,
        None,
        executor_id="executor-test",
        parameters={},
        streaming=True,
    )
    observer = executor_observer(metadata)
    assert observer is not None
    socket = Socket(eleven=True)
    monkeypatch.setattr(speech_streaming, "connect", lambda *args, **kwargs: socket)

    async def audio():
        yield b"\x01\x00" * 1600

    events = [
        e
        async for e in speech_streaming.stream_transcription(
            audio(),
            model="scribe_v2_realtime",
            provider_preset="elevenlabs",
            request_kwargs={"api_key": "test-key"},
            usage_binding=observer.binding,
        )
    ]
    assert events[-1].type == "done"
    envelope = observer.attach({})
    ingest_executor(envelope, binding, expected)
    ingest_executor(envelope, binding, expected)
    final = sink.offers[-1]
    assert len(sink.offers) == 2
    assert final.context.selection.operation == "transcribe"
    assert final.context.executor.executor_id == "executor-test"
    assert [(x.name, x.value, x.status) for x in final.usage.extras] == [
        ("input_audio_duration_ms", 100, "derived")
    ]


@pytest.mark.parametrize("old_controller", [False, True])
async def test_media_executor_opt_in_preserves_legacy_wire_shape(old_controller):
    sink = Sink()
    binding = UsageBinding(request(sink), "provider", "tts-1", media_operation="synthesize")
    metadata, expected = executor_metadata(
        binding,
        None,
        executor_id="executor-test",
        parameters={},
        streaming=False,
    )
    if old_controller:
        metadata.pop("cognis_media_usage_version")
        metadata.pop("cognis_media_operation")
    observer = executor_observer(metadata)
    assert observer is not None

    async def dispatch():
        return httpx.Response(200, content=b"audio", headers={"x-character-count": "7"})

    await observed_http(dispatch, binding=observer.binding, parameters={"input_characters": 7})
    envelope = observer.attach({})
    wire = envelope["backend_metadata"]["cognis_usage_observations"][-1]
    assert wire["context"]["selection"]["operation"] == (
        "native" if old_controller else "synthesize"
    )
    assert wire["usage"].get("provider_cost") is None
    ingest_executor(envelope, binding, expected)
    final = sink.offers[-1]
    assert final.context.selection.operation == "synthesize"
    assert {x.name: x.value for x in final.usage.extras}["input_characters"] == 7
