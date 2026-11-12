from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.models.speech import SpeechStreamEvent
from cognis.providers.llm.inference_router import InferenceRouter


def event(kind, **kwargs):
    return {"speech": SpeechStreamEvent(type=kind, **kwargs).model_dump()}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending",
    [
        [{"done": True}],
        [event("done"), {"error": "Transport lost", "done": True}],
        [event("done")],
    ],
)
async def test_executor_speech_requires_provider_and_transport_completion(monkeypatch, ending):
    closed = asyncio.Event()

    async def output(**kwargs):
        try:
            yield event("start")
            for chunk in ending:
                yield chunk
        finally:
            closed.set()

    connection = SimpleNamespace(llm_complete_stream=output, cancel_inference=AsyncMock())
    router = InferenceRouter(SimpleNamespace())
    monkeypatch.setattr(router, "_find_executor", AsyncMock(return_value=connection))
    delivered = []
    with pytest.raises(RuntimeError):
        async for chunk in router.route_speech_stream(
            operation="tts",
            model="tts-1",
            provider_preset="openai",
        ):
            delivered.append(chunk["type"])
    assert delivered == ["start"]
    assert closed.is_set()
    connection.cancel_inference.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_fails", [False, True])
async def test_executor_microphone_sender_and_receiver_share_cancellation(monkeypatch, input_fails):
    input_finished, closed = asyncio.Event(), asyncio.Event()
    observed = {}
    frames = []

    async def output(**kwargs):
        observed.update(kwargs)
        try:
            yield event("start")
            await input_finished.wait()
            yield event("transcript", text="Committed.")
            yield event("done")
            yield {"done": True}
        finally:
            closed.set()

    async def send(method, params, **kwargs):
        assert method == "llm.speech_input"
        assert params["request_id"] == observed["request_id"]
        if input_fails:
            raise OSError("Microphone delivery failed")
        frames.append(params)
        if params.get("final"):
            input_finished.set()

    async def audio():
        yield b"\0\0"

    connection = SimpleNamespace(
        llm_complete_stream=output,
        rpc_call=send,
        cancel_inference=AsyncMock(),
    )
    router = InferenceRouter(SimpleNamespace())
    monkeypatch.setattr(router, "_find_executor", AsyncMock(return_value=connection))
    stream = router.route_speech_stream(
        operation="stt",
        model="gpt-4o-transcribe",
        provider_preset="openai",
        audio=audio(),
    )
    delivered = []
    async with asyncio.timeout(2):
        async with aclosing(stream):
            if input_fails:
                with pytest.raises(OSError, match="Microphone delivery"):
                    async for chunk in stream:
                        delivered.append(chunk["type"])
            else:
                async for chunk in stream:
                    delivered.append(chunk["type"])
    assert closed.is_set()
    if input_fails:
        assert delivered == ["start"]
        connection.cancel_inference.assert_awaited_once()
    else:
        assert delivered == ["start", "transcript", "done"]
        assert frames[0]["audio_base64"] == "AAA="
        assert frames[1]["final"] is True
        connection.cancel_inference.assert_not_awaited()
