"""Native speech transport, explicit completion, and cancellation contracts."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from contextlib import aclosing

import httpx
import pytest

from cognis.core.speech import speech_chunks
from cognis.core.speech_streaming import render_speech_stream
from cognis.models.speech import MAX_SPEECH_FRAME_BYTES, SpeechStreamEvent
from cognis.models.speech import speech_streaming_capabilities as capabilities
from cognis.providers.llm import elevenlabs
from cognis.providers.llm import speech_streaming as native
from cognis.providers.llm.errors import LLMStreamProviderError


@pytest.mark.parametrize(
    "preset,model,tts,stt",
    [
        ("openai", "gpt-4o-mini-tts", True, False),
        ("openai", "tts-1", True, False),
        ("openai", "gpt-4o-mini-transcribe", False, True),
        ("openai", "gpt-live-transcribe", False, True),
        ("openai", "gpt-transcribe", False, True),
        ("openai", "gpt-4o-transcribe-diarize", False, False),
        ("openai", "whisper-1", False, False),
        ("elevenlabs", "eleven_v4_turbo", True, False),
        ("elevenlabs", "eleven_multilingual_v2", True, False),
        ("elevenlabs", "scribe_v2_realtime", False, True),
        ("elevenlabs", "scribe_v2", False, False),
        ("anthropic", "gpt-4o-mini-tts", False, False),
    ],
)
def test_native_capabilities_do_not_substitute_models(preset, model, tts, stt):
    result = capabilities(preset, model)
    assert (result.tts, result.stt) == (tts, stt)


class Bytes(httpx.AsyncByteStream):
    async def __aiter__(self):
        for chunk in (b"\x01", b"\x00\x02", b"\x00"):
            yield chunk


@pytest.mark.parametrize(
    "preset,model,path",
    [
        ("openai", "gpt-4o-mini-tts", "/v1/audio/speech"),
        ("elevenlabs", "eleven_multilingual_v2", "/v1/text-to-speech/voice-one/stream"),
    ],
)
async def test_http_tts_streams_even_pcm_frames_once(monkeypatch, preset, model, path):
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=Bytes())

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(native.httpx, "AsyncClient", lambda **kwargs: client)
    events = [
        event
        async for event in native.stream_synthesis(
            provider_preset=preset,
            model=model,
            text="Spoken text",
            voice="voice-one",
            request_kwargs={"api_key": "test-key"},
        )
    ]
    assert len(requests) == 1
    assert requests[0].url.path == path
    assert [event.type for event in events] == ["start", "audio", "audio", "done"]
    assert (
        b"".join(base64.b64decode(e.audio_base64) for e in events if e.type == "audio")
        == b"\x01\x00\x02\x00"
    )
    assert events[-1].sample_offset == 2


class Socket:
    def __init__(self, eleven=False, dialogue=False):
        self.eleven, self.dialogue = eleven, dialogue
        self.messages = []
        self.incoming = asyncio.Queue()
        self.commits = 0
        self.closed = False
        if eleven and not dialogue:
            self.incoming.put_nowait(json.dumps({"message_type": "session_started"}))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)
        if self.dialogue and message.get("close_socket"):
            self.incoming.put_nowait(
                json.dumps(
                    {
                        "audio": base64.b64encode(b"\x01\x00").decode(),
                        "is_final_audio_for_turn": True,
                    }
                )
            )
            self.incoming.put_nowait(json.dumps({"is_final": True}))
        elif message.get("type") == "session.update":
            self.incoming.put_nowait(json.dumps({"type": "session.updated"}))
        elif message.get("commit") or message.get("type") == "input_audio_buffer.commit":
            self.commits += 1
            if self.eleven:
                event = {"message_type": "committed_transcript", "text": f"Segment {self.commits}"}
            else:
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.committed",
                            "item_id": "utterance-one",
                        }
                    )
                )
                event = {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "utterance-one",
                    "transcript": "A complete transcript.",
                }
            self.incoming.put_nowait(json.dumps(event))

    async def recv(self):
        return await self.incoming.get()


def _recording(*, rate: int = 48000, channels: int = 2, samples: int = 4800) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x01\x00" * channels * samples)
    return buffer.getvalue()


@pytest.mark.parametrize("base", ["https://api.elevenlabs.io", "https://api.elevenlabs.io/v1"])
@pytest.mark.parametrize("container", ["wav", "mp3", "mp4"])
async def test_realtime_scribe_transcribes_recordings_without_model_substitution(
    monkeypatch, tmp_path, base, container
):
    content = _recording()
    if container != "wav":
        path = tmp_path / f"recording.{container}"
        encoder = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "wav",
            "-i",
            "pipe:0",
            "-f",
            container,
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await encoder.communicate(content)
        assert encoder.returncode == 0, stderr
        content = path.read_bytes()
    socket = Socket(eleven=True)
    connections = []

    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return socket

    async def no_multipart(*args, **kwargs):
        pytest.fail("Realtime Scribe must not use multipart batch transcription")

    monkeypatch.setattr(native, "connect", connect)
    monkeypatch.setattr(httpx.AsyncClient, "post", no_multipart)
    result = await elevenlabs.transcribe(
        audio_bytes=content,
        mime_type=f"audio/{container}",
        filename=f"voice.{container}",
        model="scribe_v2_realtime",
        language="cs",
        prompt=None,
        request_kwargs={"api_key": "test-key", "api_base": base},
    )
    assert result.model == "scribe_v2_realtime"
    assert result.text == "Segment 1"
    assert result.language == "cs"
    assert len(connections) == 1
    url, kwargs = connections[0]
    assert url.startswith("wss://api.elevenlabs.io/v1/speech-to-text/realtime?")
    assert "model_id=scribe_v2_realtime" in url and "language_code=cs" in url
    assert kwargs["additional_headers"] == {"xi-api-key": "test-key"}
    frames = [
        base64.b64decode(message["audio_base_64"])
        for message in socket.messages
        if message.get("audio_base_64")
    ]
    assert frames and all(
        len(frame) % 2 == 0 and len(frame) <= MAX_SPEECH_FRAME_BYTES for frame in frames
    )
    if container == "wav":
        assert sum(map(len, frames)) == 3200
    assert socket.commits == 1 and socket.closed


@pytest.mark.parametrize(
    "failure", ["empty", "encoded_limit", "decoded_limit", "corrupt", "missing_ffmpeg"]
)
async def test_realtime_recording_is_validated_before_provider_dispatch(monkeypatch, failure):
    content = _recording(rate=8000, channels=1, samples=1000)
    if failure == "empty":
        content = b""
    elif failure == "encoded_limit":
        monkeypatch.setattr(elevenlabs, "MAX_SPEECH_INPUT_BYTES", 100)
    elif failure == "decoded_limit":
        monkeypatch.setattr(elevenlabs, "MAX_SPEECH_INPUT_BYTES", 3000)
    elif failure == "corrupt":
        content = b"not an audio container"
    else:
        monkeypatch.setattr(elevenlabs.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        native, "connect", lambda *args, **kwargs: pytest.fail("Unexpected provider dispatch")
    )
    with pytest.raises((ValueError, RuntimeError)):
        await elevenlabs.transcribe(
            audio_bytes=content,
            mime_type="audio/wav",
            filename="voice.wav",
            model="scribe_v2_realtime",
            language=None,
            prompt=None,
            request_kwargs={"api_key": "test-key"},
        )


async def test_realtime_recording_decode_cancellation_reaps_owned_process(monkeypatch):
    started = asyncio.Event()
    paths = []

    class Decoder:
        returncode = None
        waited = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.returncode = -9

        async def wait(self):
            self.waited = True

    decoder = Decoder()

    async def spawn(*args, **kwargs):
        paths.append(args[args.index("-i") + 1])
        return decoder

    monkeypatch.setattr(elevenlabs.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        elevenlabs.transcribe(
            audio_bytes=_recording(),
            mime_type="audio/wav",
            filename="voice.wav",
            model="scribe_v2_realtime",
            language=None,
            prompt=None,
            request_kwargs={"api_key": "test-key"},
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    from pathlib import Path

    assert decoder.returncode == -9 and decoder.waited
    assert paths and not Path(paths[0]).exists()


@pytest.mark.parametrize("base_key", ["api_base", "base_url"])
def test_native_elevenlabs_normalizes_unversioned_api_bases(base_key):
    url, _ = native._connection(
        "elevenlabs",
        {base_key: "https://api.elevenlabs.io", "api_key": "test-key"},
        "speech-to-text/realtime",
        websocket=True,
    )
    assert url == "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


async def test_eleven_dialogue_uses_registered_voice_and_final_marker(monkeypatch):
    socket = Socket(eleven=True, dialogue=True)
    monkeypatch.setattr(native, "connect", lambda *args, **kwargs: socket)
    events = [
        event
        async for event in native.stream_synthesis(
            provider_preset="elevenlabs",
            model="eleven_v4_turbo",
            text="Speech",
            voice="voice-one",
            request_kwargs={"api_key": "test-key"},
        )
    ]
    assert socket.messages[0]["voices"] == ["voice-one"]
    assert socket.messages[-1] == {"close_socket": True}
    assert events[-1].type == "done"
    assert socket.closed


@pytest.mark.parametrize(
    ("error", "category", "code"),
    [
        ({"error": "quota_exceeded"}, "quota_exhausted", "quota_exceeded"),
        (
            {"error": {"status": "invalid_api_key", "message": "Private input and credentials"}},
            "invalid_request",
            "invalid_api_key",
        ),
        ({"type": "error", "code": "rate_limit_exceeded"}, "rate_limit", "rate_limit_exceeded"),
        ({"error": {"message": "Private input and credentials"}}, "other", "speech_provider_error"),
        ({"error": ["Private input and credentials"]}, "other", "speech_provider_error"),
    ],
)
async def test_eleven_dialogue_preserves_safe_rejection_metadata_once(
    monkeypatch, error, category, code
):
    socket = Socket(eleven=True, dialogue=True)
    socket.incoming.put_nowait(json.dumps(error))
    connections = []

    def connect(*args, **kwargs):
        connections.append(args)
        return socket

    monkeypatch.setattr(native, "connect", connect)
    with pytest.raises(LLMStreamProviderError) as failure:
        async for _ in native.stream_synthesis(
            provider_preset="elevenlabs",
            model="eleven_v4_turbo",
            text="Private input and credentials",
            voice="voice-one",
            request_kwargs={"api_key": "test-key"},
        ):
            pass
    assert failure.value.payload["category"] == category
    assert failure.value.payload["code"] == code
    assert "Private" not in str(failure.value.payload)
    assert socket.closed and len(connections) == 1


@pytest.mark.parametrize(
    "preset,model",
    [
        ("elevenlabs", "scribe_v2_realtime"),
        ("openai", "gpt-4o-transcribe"),
        ("openai", "gpt-live-transcribe"),
    ],
)
async def test_live_stt_commits_once_and_waits_for_confirmation(monkeypatch, preset, model):
    socket = Socket(eleven=preset == "elevenlabs")
    monkeypatch.setattr(native, "connect", lambda *args, **kwargs: socket)

    async def audio():
        yield b"\x00\x00" * 4096

    events = [
        event
        async for event in native.stream_transcription(
            audio(),
            provider_preset=preset,
            model=model,
            request_kwargs={"api_key": "test-key"},
            language="en",
        )
    ]
    assert socket.commits == 1
    assert [e.type for e in events][-2:] == ["transcript", "done"]
    assert events[-2].text
    if preset == "openai":
        session = socket.messages[0]["session"]
        assert socket.messages[0]["type"] == "session.update"
        assert session["audio"]["input"]["transcription"]["model"] == model
        assert session["audio"]["input"]["turn_detection"] is None
    assert socket.closed


async def test_eleven_serializes_commits_before_automatic_boundary(monkeypatch):
    socket = Socket(eleven=True)
    monkeypatch.setattr(native, "connect", lambda *args, **kwargs: socket)

    async def audio():
        remaining = 17 * 16000 * 2
        while remaining:
            length = min(remaining, MAX_SPEECH_FRAME_BYTES)
            remaining -= length
            yield b"\0" * length

    events = [
        event
        async for event in native.stream_transcription(
            audio(),
            provider_preset="elevenlabs",
            model="scribe_v2_realtime",
            request_kwargs={"api_key": "test-key"},
        )
    ]
    assert socket.commits == 2
    assert events[-2].text == "Segment 1 Segment 2"


async def test_live_input_failure_closes_socket_without_replay(monkeypatch):
    socket = Socket(eleven=True)
    calls = []

    def connect(*args, **kwargs):
        calls.append(args)
        return socket

    monkeypatch.setattr(native, "connect", connect)

    async def audio():
        yield b"\x00"

    with pytest.raises(ValueError, match="PCM frame"):
        _ = [
            event
            async for event in native.stream_transcription(
                audio(),
                provider_preset="elevenlabs",
                model="scribe_v2_realtime",
                request_kwargs={"api_key": "test-key"},
            )
        ]
    assert len(calls) == 1 and socket.closed


async def test_live_cancellation_joins_the_single_input_owner(monkeypatch):
    socket = Socket(eleven=True)
    monkeypatch.setattr(native, "connect", lambda *args, **kwargs: socket)
    started, closed = asyncio.Event(), asyncio.Event()

    async def audio():
        try:
            started.set()
            await asyncio.Event().wait()
            yield b"\0\0"
        finally:
            closed.set()

    async def consume():
        async with aclosing(
            native.stream_transcription(
                audio(),
                provider_preset="elevenlabs",
                model="scribe_v2_realtime",
                request_kwargs={"api_key": "test-key"},
            )
        ) as stream:
            async for _ in stream:
                pass

    task = asyncio.create_task(consume())
    gate = asyncio.create_task(started.wait())
    await asyncio.wait({task, gate}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        await task
    await gate
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and socket.closed


@pytest.mark.parametrize(
    "failure", ["missing_done", "offset", "rate", "trailing", "duplicate_start"]
)
async def test_incomplete_or_invalid_audio_is_not_finalized(failure):
    events = [
        SpeechStreamEvent(type="start"),
        SpeechStreamEvent(type="audio", sample_offset=0, audio_base64="AAA="),
        SpeechStreamEvent(type="done", sample_offset=1),
    ]
    if failure == "missing_done":
        events.pop()
    elif failure == "offset":
        events[1].sample_offset = 2
    elif failure == "rate":
        events[0].sample_rate = 16000
    elif failure == "trailing":
        events.append(events[1])
    else:
        events.insert(1, events[0])

    async def stream():
        for event in events:
            yield event.model_dump()

    with pytest.raises((ValueError, RuntimeError)):
        await render_speech_stream(stream(), format="pcm", model="tts-1", voice="alloy")


def test_progressive_first_part_does_not_drop_source_text():
    text = "First short sentence. " + "Remaining source. " * 500
    parts = speech_chunks(text, first_limit=240)
    assert len(parts[0]) <= 240 and max(map(len, parts)) <= 3500
    assert " ".join(parts).split() == text.split()
