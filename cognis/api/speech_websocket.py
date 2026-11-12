"""Live microphone adaptation on the authenticated, revocable browser socket."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from typing import Any, Protocol

from cognis.core.artifact_revisions import ContentActor
from cognis.core.speech import SpeechService
from cognis.models.speech import MAX_SPEECH_FRAME_BYTES, MAX_SPEECH_INPUT_BYTES


class LiveTranscription:
    """Own one non-replayable utterance for one authenticated connection."""

    def __init__(
        self,
        service: SpeechService,
        actor: ContentActor,
        request_id: str,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        language: str | None = None,
    ) -> None:
        self.request_id = request_id
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=16)
        self._total = 0
        self._committed = False
        self._task = asyncio.create_task(self._run(service, actor, send, language))

    async def _run(
        self,
        service: SpeechService,
        actor: ContentActor,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        language: str | None,
    ) -> None:
        async def audio() -> AsyncIterator[bytes]:
            while (frame := await self._queue.get()) is not None:
                yield frame

        try:
            async with aclosing(
                service.transcribe_live(actor, audio(), language=language)
            ) as events:
                async for event in events:
                    await send(
                        {
                            "type": "speech_stt_event",
                            "request_id": self.request_id,
                            "event": event,
                        }
                    )
        except Exception:
            await send(
                {
                    "type": "speech_stt_error",
                    "request_id": self.request_id,
                    "code": "speech_stream_failed",
                }
            )

    def feed(self, message: dict[str, Any]) -> None:
        """Validate microphone input before admitting it to the provider queue."""
        if self._task.done() or self._committed:
            raise ValueError("Microphone stream is closed")
        if message["type"] == "speech_stt_commit":
            self._queue.put_nowait(None)
            self._committed = True
            return
        encoded = message.get("audio_base64")
        if not isinstance(encoded, str) or len(encoded) > MAX_SPEECH_FRAME_BYTES * 4 // 3 + 4:
            raise ValueError("Microphone frame is too large")
        frame = base64.b64decode(encoded, validate=True)
        if not frame or len(frame) % 2 or len(frame) > MAX_SPEECH_FRAME_BYTES:
            raise ValueError("Invalid microphone PCM frame")
        self._total += len(frame)
        if self._total > MAX_SPEECH_INPUT_BYTES:
            raise ValueError("Microphone input is too large")
        self._queue.put_nowait(frame)

    async def close(self) -> None:
        """Disconnect or revocation cancels the provider and releases its input."""
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class SpeechConnection(Protocol):
    user_email: str
    role: str
    speech_transcription: LiveTranscription | None

    async def send_json(self, data: dict[str, Any]) -> None: ...


async def handle_speech_message(
    app: Any, connection: SpeechConnection, message: dict[str, Any]
) -> None:
    """Adapt one validated connection's microphone command without fanout."""
    request_id = message.get("request_id")
    if connection.role == "viewer":
        await connection.send_json(
            {
                "type": "speech_stt_error",
                "request_id": request_id,
                "code": "forbidden",
            }
        )
        return
    current = connection.speech_transcription
    try:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            raise ValueError("Invalid speech request ID")
        if message["type"] == "speech_stt_start":
            if current is not None:
                raise ValueError("A microphone stream is already active")
            language = message.get("language")
            if language is not None and (not isinstance(language, str) or len(language) > 16):
                raise ValueError("Invalid speech language")
            connection.speech_transcription = LiveTranscription(
                SpeechService(
                    session_factory=app.state.session_factory,
                    artifacts=app.state.artifact_store,
                    llm=app.state.providers.llm,
                ),
                ContentActor(connection.user_email, None, None, None, human=True),
                request_id,
                connection.send_json,
                language,
            )
        elif current is None or current.request_id != request_id:
            raise ValueError("Speech request is no longer active")
        elif message["type"] == "speech_stt_cancel":
            await current.close()
            connection.speech_transcription = None
        else:
            current.feed(message)
    except (ValueError, asyncio.QueueFull):
        if current is not None and current.request_id == request_id:
            await current.close()
            connection.speech_transcription = None
        await connection.send_json(
            {
                "type": "speech_stt_error",
                "request_id": request_id,
                "code": "invalid_speech_input",
            }
        )
