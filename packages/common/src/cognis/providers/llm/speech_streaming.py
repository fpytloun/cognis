"""Native, no-replay speech protocols shared by controller and executor."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect

from cognis.models.speech import (
    MAX_SPEECH_FRAME_BYTES,
    MAX_SPEECH_INPUT_BYTES,
    PCM_SAMPLE_RATE,
    SpeechStreamEvent,
    speech_streaming_capabilities,
)
from cognis.providers.llm.errors import LLMStreamProviderError
from cognis.providers.llm.hosted import api_endpoint
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_capture import UsageAttempt


@asynccontextmanager
async def _speech_observation(binding: UsageBinding | None) -> AsyncIterator[UsageAttempt | None]:
    """Own one physical stream, including cancellation and early consumer closure."""
    attempt = binding.begin(operation="native", streaming=True, parameters={}) if binding else None
    try:
        yield attempt
    except BaseException as exc:
        if attempt is not None:
            attempt.finish(error=exc)
        raise
    finally:
        if attempt is not None and not attempt.finished:
            attempt.finish(outcome="unknown")


def _elevenlabs_stream_error(event: dict[str, Any]) -> LLMStreamProviderError:
    """Retain known error codes without exposing provider text or request data."""
    error = event.get("error")
    detail = error if isinstance(error, dict) else event
    code = detail.get("code") or detail.get("status") or error
    categories = {
        "quota_exceeded": "quota_exhausted",
        "insufficient_credits": "quota_exhausted",
        "rate_limit_exceeded": "rate_limit",
        "too_many_concurrent_requests": "rate_limit",
        "system_busy": "provider_5xx",
        "invalid_api_key": "invalid_request",
        "missing_permissions": "invalid_request",
        "unauthorized": "invalid_request",
        "model_not_found": "invalid_request",
        "voice_not_found": "invalid_request",
        "invalid_voice_id": "invalid_request",
        "invalid_request": "invalid_request",
        "input_timeout_exceeded": "connection",
    }
    code = code if isinstance(code, str) and code in categories else "speech_provider_error"
    return LLMStreamProviderError(
        "ElevenLabs rejected speech synthesis",
        payload={
            "category": categories.get(code, "other"),
            "code": code,
            "message": "ElevenLabs rejected speech synthesis",
        },
    )


def _connection(
    preset: str | None, request_kwargs: dict[str, Any], path: str, *, websocket: bool = False
) -> tuple[str, dict[str, str]]:
    eleven = preset == "elevenlabs"
    key = request_kwargs.get("api_key")
    if not isinstance(key, str) or not key:
        raise ValueError("Native speech streaming requires a resolved provider credential")
    base = str(
        request_kwargs.get("api_base")
        or request_kwargs.get("base_url")
        or ("https://api.elevenlabs.io/v1" if eleven else "https://api.openai.com/v1")
    ).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query:
        raise ValueError("Unsupported speech API base")
    scheme = ("wss" if parsed.scheme == "https" else "ws") if websocket else parsed.scheme
    target = (
        urlsplit(api_endpoint(base, path)).path if eleven else f"{parsed.path}/{path.lstrip('/')}"
    )
    url = urlunsplit((scheme, parsed.netloc, target, "", ""))
    headers = {"xi-api-key": key} if eleven else {"Authorization": f"Bearer {key}"}
    return url, headers


class _PcmEvents:
    def __init__(self, model: str, voice: str) -> None:
        self.model = model
        self.voice = voice
        self.offset = 0
        self.remainder = b""

    def audio(self, data: bytes) -> list[SpeechStreamEvent]:
        data = self.remainder + data
        self.remainder = data[len(data) - len(data) % 2 :]
        data = data[: len(data) - len(data) % 2]
        events = []
        for start in range(0, len(data), MAX_SPEECH_FRAME_BYTES):
            frame = data[start : start + MAX_SPEECH_FRAME_BYTES]
            events.append(
                SpeechStreamEvent(
                    type="audio",
                    model=self.model,
                    voice=self.voice,
                    audio_base64=base64.b64encode(frame).decode("ascii"),
                    sample_offset=self.offset,
                )
            )
            self.offset += len(frame) // 2
        return events

    def done(self) -> SpeechStreamEvent:
        if self.remainder or not self.offset:
            raise ValueError("Provider returned incomplete or empty PCM audio")
        return SpeechStreamEvent(
            type="done",
            model=self.model,
            voice=self.voice,
            sample_offset=self.offset,
            duration_seconds=self.offset / PCM_SAMPLE_RATE,
        )


async def stream_synthesis(
    *,
    text: str,
    model: str,
    voice: str,
    provider_preset: str | None,
    request_kwargs: dict[str, Any],
    speed: float = 1,
    usage_binding: UsageBinding | None = None,
) -> AsyncGenerator[SpeechStreamEvent, None]:
    """Yield native PCM before completion; never retry a dispatched request."""
    if not speech_streaming_capabilities(provider_preset, model).tts:
        raise ValueError("This model does not support native streaming TTS")
    name = model.rsplit("/", 1)[-1]
    pcm = _PcmEvents(model, voice)
    timeout = float(request_kwargs.get("timeout") or 120)
    async with _speech_observation(usage_binding) as attempt, asyncio.timeout(timeout):
        if provider_preset == "elevenlabs" and name.startswith(("eleven_v3", "eleven_v4")):
            url, headers = _connection(
                provider_preset, request_kwargs, "text-to-dialogue/stream-input", websocket=True
            )
            url += "?" + urlencode({"model_id": name, "output_format": "pcm_24000"})
            async with connect(
                url, additional_headers=headers, max_size=2 * 1024 * 1024, open_timeout=30
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "voices": [voice],
                            "voice_settings": {"speed": max(0.7, min(1.2, speed))},
                        }
                    )
                )
                await socket.send(
                    json.dumps({"inputs": [{"text": text, "voice_id": voice}], "flush": True})
                )
                if attempt is not None:
                    attempt.accumulator.report_extra(
                        "input_characters", len(text), source="adapter", derived=True
                    )
                await socket.send(json.dumps({"close_socket": True}))
                yield SpeechStreamEvent(type="start", model=model, voice=voice)
                finished = False
                async for raw in socket:
                    event = json.loads(raw)
                    if attempt is not None:
                        attempt.observe(event, generated=bool(event.get("audio")))
                    if event.get("error") or event.get("type") == "error":
                        raise _elevenlabs_stream_error(event)
                    if event.get("audio"):
                        for frame in pcm.audio(base64.b64decode(event["audio"], validate=True)):
                            yield frame
                    if event.get("is_final"):
                        finished = True
                        break
                if not finished:
                    raise LLMStreamProviderError(
                        "ElevenLabs synthesis ended without final audio",
                        payload={
                            "category": "protocol",
                            "code": "speech_stream_incomplete",
                            "message": "ElevenLabs synthesis ended without final audio",
                        },
                    )
        else:
            eleven = provider_preset == "elevenlabs"
            path = f"text-to-speech/{quote(voice, safe='')}/stream" if eleven else "audio/speech"
            url, headers = _connection(provider_preset, request_kwargs, path)
            body: dict[str, Any]
            if eleven:
                url += "?output_format=pcm_24000"
                body = {
                    "text": text,
                    "model_id": name,
                    "voice_settings": {"speed": max(0.7, min(1.2, speed))},
                }
            else:
                body = {
                    "input": text,
                    "model": name,
                    "voice": voice,
                    "speed": speed,
                    "response_format": "pcm",
                }
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream("POST", url, headers=headers, json=body) as response,
            ):
                response.raise_for_status()
                if attempt is not None:
                    attempt.accumulator.report_extra(
                        "input_characters", len(text), source="adapter", derived=True
                    )
                    characters = response.headers.get("x-character-count")
                    if characters is not None and characters.isdecimal() and len(characters) <= 18:
                        attempt.accumulator.report_extra("input_characters", int(characters))
                yield SpeechStreamEvent(type="start", model=model, voice=voice)
                async for data in response.aiter_bytes():
                    for frame in pcm.audio(data):
                        yield frame
        done = pcm.done()
        if attempt is not None:
            attempt.accumulator.report_extra(
                "output_audio_duration_ms",
                round(pcm.offset * 1000 / PCM_SAMPLE_RATE),
                source="adapter",
                derived=True,
            )
            attempt.finish()
        yield done


async def stream_transcription(
    audio: AsyncIterator[bytes],
    *,
    model: str,
    provider_preset: str | None,
    request_kwargs: dict[str, Any],
    language: str | None = None,
    usage_binding: UsageBinding | None = None,
) -> AsyncGenerator[SpeechStreamEvent, None]:
    """Transcribe one explicitly committed microphone utterance without replay."""
    capabilities = speech_streaming_capabilities(provider_preset, model)
    if not capabilities.stt:
        raise ValueError("This model does not support live transcription")
    eleven = provider_preset == "elevenlabs"
    name = model.rsplit("/", 1)[-1]
    path = "speech-to-text/realtime" if eleven else "realtime"
    url, headers = _connection(provider_preset, request_kwargs, path, websocket=True)
    query = (
        {"model_id": name, "audio_format": "pcm_16000", "commit_strategy": "manual"}
        if eleven
        else {"intent": "transcription"}
    )
    if eleven and language:
        query["language_code"] = language
    url += "?" + urlencode(query)
    timeout = float(request_kwargs.get("timeout") or 90)
    async with (
        _speech_observation(usage_binding) as attempt,
        asyncio.timeout(timeout),
        connect(url, additional_headers=headers, max_size=1024 * 1024, open_timeout=30) as socket,
    ):
        if not eleven:
            await socket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "transcription",
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "transcription": {
                                        "model": name,
                                        **(
                                            {"languages": [language]}
                                            if language and name == "gpt-live-transcribe"
                                            else {"language": language}
                                            if language
                                            else {}
                                        ),
                                    },
                                    "turn_detection": None,
                                }
                            },
                        },
                    }
                )
            )

        while True:
            event = json.loads(await socket.recv())
            kind = event.get("message_type") if eleven else event.get("type")
            if kind == "error" or event.get("error"):
                raise RuntimeError("Provider live transcription configuration failed")
            if kind == ("session_started" if eleven else "session.updated"):
                break
        committed: list[str] = []
        acknowledgement: asyncio.Future[None] | None = None
        committed_item: str | None = None
        partial = ""

        async def commit() -> None:
            nonlocal acknowledgement
            acknowledgement = asyncio.get_running_loop().create_future()
            await socket.send(
                json.dumps(
                    {"message_type": "input_audio_chunk", "audio_base_64": "", "commit": True}
                    if eleven
                    else {"type": "input_audio_buffer.commit"}
                )
            )
            await acknowledgement

        async def send_audio() -> None:
            total = 0
            sent_bytes = 0
            segment_bytes = 0
            segment_limit = capabilities.sample_rate * 2 * 16
            async for frame in audio:
                if not frame or len(frame) % 2 or len(frame) > MAX_SPEECH_FRAME_BYTES:
                    raise ValueError("Invalid microphone PCM frame")
                total += len(frame)
                if total > MAX_SPEECH_INPUT_BYTES:
                    raise ValueError("Microphone input exceeds the speech limit")
                while frame:
                    # Serialize explicit commits well before Eleven's automatic
                    # ~36-second boundary; its protocol supplies no commit IDs.
                    count = min(len(frame), segment_limit - segment_bytes) if eleven else len(frame)
                    encoded = base64.b64encode(frame[:count]).decode("ascii")
                    frame = frame[count:]
                    message = (
                        {"message_type": "input_audio_chunk", "audio_base_64": encoded}
                        if eleven
                        else {"type": "input_audio_buffer.append", "audio": encoded}
                    )
                    await socket.send(json.dumps(message))
                    sent_bytes += count
                    if attempt is not None:
                        attempt.accumulator.report_extra(
                            "input_audio_duration_ms",
                            round(sent_bytes * 1000 / (capabilities.sample_rate * 2)),
                            source="adapter",
                            derived=True,
                        )
                    segment_bytes += count
                    if eleven and segment_bytes == segment_limit:
                        await commit()
                        segment_bytes = 0
            if not total:
                raise ValueError("No microphone audio was received")
            if segment_bytes:
                await commit()

        sender = asyncio.create_task(send_audio())
        try:
            yield SpeechStreamEvent(type="start", model=model, sample_rate=capabilities.sample_rate)
            receive: asyncio.Task[Any] | None = None
            while True:
                receive = asyncio.create_task(socket.recv())
                done, _ = await asyncio.wait({sender, receive}, return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                    await sender
                    if attempt is not None:
                        attempt.finish()
                    yield SpeechStreamEvent(
                        type="transcript",
                        model=model,
                        sample_rate=capabilities.sample_rate,
                        text=" ".join(part for part in committed if part).strip(),
                        utterance_id=committed_item or "",
                    )
                    yield SpeechStreamEvent(
                        type="done", model=model, sample_rate=capabilities.sample_rate
                    )
                    return
                raw = await receive
                event = json.loads(raw)
                if attempt is not None:
                    attempt.observe(event, generated=False)
                kind = event.get("message_type") if eleven else event.get("type")
                if kind == "error" or event.get("error"):
                    raise RuntimeError("Provider live transcription failed")
                if kind == "input_audio_buffer.committed":
                    committed_item = str(event["item_id"])
                    continue
                if kind == "conversation.item.input_audio_transcription.failed":
                    raise RuntimeError("Provider live transcription failed")
                if kind in {
                    "partial_transcript",
                    "conversation.item.input_audio_transcription.delta",
                }:
                    partial = (
                        str(event.get("text", ""))
                        if eleven
                        else partial + str(event.get("delta", ""))
                    )
                    yield SpeechStreamEvent(
                        type="partial",
                        model=model,
                        sample_rate=capabilities.sample_rate,
                        text=" ".join([*committed, partial]).strip(),
                        utterance_id=str(event.get("item_id") or ""),
                    )
                elif kind in {
                    "committed_transcript",
                    "conversation.item.input_audio_transcription.completed",
                }:
                    if acknowledgement is None or acknowledgement.done():
                        raise RuntimeError("Unexpected provider transcription commit")
                    if not eleven and (
                        not committed_item or event.get("item_id") != committed_item
                    ):
                        raise RuntimeError("Provider transcript does not match its committed input")
                    text = str(
                        event.get("text") if eleven else event.get("transcript") or ""
                    ).strip()
                    committed.append(text)
                    partial = ""
                    acknowledgement.set_result(None)
                    if eleven:
                        yield SpeechStreamEvent(
                            type="partial",
                            model=model,
                            sample_rate=capabilities.sample_rate,
                            text=" ".join(part for part in committed if part),
                        )
        finally:
            sender.cancel()
            if receive is not None and not receive.done():
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            await asyncio.gather(sender, return_exceptions=True)
