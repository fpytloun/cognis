from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api import speech_websocket
from cognis.models.speech import MAX_SPEECH_FRAME_BYTES, SpeechStreamEvent


@pytest.mark.asyncio
async def test_viewer_cannot_start_paid_transcription():
    connection = SimpleNamespace(role="viewer", send_json=AsyncMock())
    await speech_websocket.handle_speech_message(
        SimpleNamespace(),
        connection,
        {"type": "speech_stt_start", "request_id": "viewer-utterance"},
    )
    connection.send_json.assert_awaited_once_with(
        {
            "type": "speech_stt_error",
            "request_id": "viewer-utterance",
            "code": "forbidden",
        }
    )


@pytest.fixture
def live_connection(monkeypatch):
    sent = asyncio.Queue()
    closed = asyncio.Event()
    actors, frames = [], []

    class Service:
        def __init__(self, **kwargs):
            pass

        async def transcribe_live(self, actor, audio, **kwargs):
            actors.append(actor)
            try:
                yield SpeechStreamEvent(type="start").model_dump()
                async for frame in audio:
                    frames.append(frame)
                yield SpeechStreamEvent(type="transcript", text="Committed words.").model_dump()
                yield SpeechStreamEvent(type="done").model_dump()
            finally:
                closed.set()

    monkeypatch.setattr(speech_websocket, "SpeechService", Service)
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=None,
            artifact_store=None,
            providers=SimpleNamespace(llm=None),
        )
    )
    connection = SimpleNamespace(
        role="user",
        user_email="listener@cognis.test",
        speech_transcription=None,
        send_json=sent.put,
    )
    return app, connection, sent, closed, actors, frames


async def start(live_connection):
    app, connection, sent, *_ = live_connection
    await speech_websocket.handle_speech_message(
        app,
        connection,
        {
            "type": "speech_stt_start",
            "request_id": "owned-utterance",
            "user_email": "another-user@cognis.test",
        },
    )
    async with asyncio.timeout(2):
        assert (await sent.get())["event"]["type"] == "start"


@pytest.mark.asyncio
async def test_live_input_uses_authenticated_identity_and_committed_completion(live_connection):
    app, connection, sent, closed, actors, frames = live_connection
    await start(live_connection)
    try:
        assert actors[0].user_email == connection.user_email
        await speech_websocket.handle_speech_message(
            app,
            connection,
            {
                "type": "speech_stt_cancel",
                "request_id": "another-utterance",
            },
        )
        assert (await sent.get())["code"] == "invalid_speech_input"
        assert connection.speech_transcription is not None
        await speech_websocket.handle_speech_message(
            app,
            connection,
            {
                "type": "speech_stt_audio",
                "request_id": "owned-utterance",
                "audio_base64": "AAA=",
            },
        )
        await speech_websocket.handle_speech_message(
            app,
            connection,
            {
                "type": "speech_stt_commit",
                "request_id": "owned-utterance",
            },
        )
        async with asyncio.timeout(2):
            assert (await sent.get())["event"]["text"] == "Committed words."
            assert (await sent.get())["event"]["type"] == "done"
            await closed.wait()
        assert frames == [b"\0\0"]
    finally:
        await connection.speech_transcription.close()


@pytest.mark.asyncio
async def test_live_input_cancellation_joins_the_provider(live_connection):
    app, connection, sent, closed, _, _ = live_connection
    await start(live_connection)
    await speech_websocket.handle_speech_message(
        app,
        connection,
        {
            "type": "speech_stt_cancel",
            "request_id": "owned-utterance",
        },
    )
    assert closed.is_set()
    assert connection.speech_transcription is None
    assert sent.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "audio",
    [
        "%%%",
        base64.b64encode(b"x").decode(),
        base64.b64encode(b"\0" * (MAX_SPEECH_FRAME_BYTES + 2)).decode(),
    ],
)
async def test_invalid_live_pcm_closes_before_publication(live_connection, audio):
    app, connection, sent, closed, _, frames = live_connection
    await start(live_connection)
    await speech_websocket.handle_speech_message(
        app,
        connection,
        {
            "type": "speech_stt_audio",
            "request_id": "owned-utterance",
            "audio_base64": audio,
        },
    )
    assert (await sent.get())["code"] == "invalid_speech_input"
    assert closed.is_set()
    assert connection.speech_transcription is None
    assert not frames
