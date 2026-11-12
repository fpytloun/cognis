"""Single-attempt speech streams over the existing inference event channel."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any

from cognis.models.speech import MAX_SPEECH_FRAME_BYTES, SpeechStreamEvent
from cognis.providers.llm.speech_streaming import stream_synthesis, stream_transcription
from cognis.providers.llm.usage_binding import UsageBinding


class ExecutorSpeechStreams:
    """Own bounded microphone queues for active inference requests only."""

    def __init__(self) -> None:
        self._inputs: dict[str, asyncio.Queue[bytes | None]] = {}
        self._committed: set[str] = set()

    def feed(self, request_id: str, encoded: str, final: bool) -> None:
        """Accept one frame once; overload fails instead of discarding audio."""
        queue = self._inputs.get(request_id)
        if queue is None or request_id in self._committed:
            raise ValueError("No open microphone stream")
        if len(encoded) > MAX_SPEECH_FRAME_BYTES * 4 // 3 + 4:
            raise ValueError("Microphone frame is too large")
        data = base64.b64decode(encoded, validate=True)
        if final and data or not final and (not data or len(data) % 2):
            raise ValueError("Invalid microphone frame")
        queue.put_nowait(None if final else data)
        if final:
            self._committed.add(request_id)

    async def stream(
        self, params: dict[str, Any], *, usage_binding: UsageBinding | None = None
    ) -> AsyncGenerator[SpeechStreamEvent, None]:
        """Run native speech without retries or controller dependencies."""
        request_id = str(params["request_id"])
        model = str(params["model"])
        preset = params.get("provider_preset")
        request_kwargs = dict(params.get("request_kwargs") or {})
        if params["speech_operation"] == "tts":
            stream = stream_synthesis(
                text=str(params["text"]),
                voice=str(params["voice"]),
                speed=float(params.get("speed", 1)),
                model=model,
                provider_preset=preset,
                request_kwargs=request_kwargs,
                usage_binding=usage_binding,
            )
        elif params["speech_operation"] == "stt":
            if request_id in self._inputs:
                raise ValueError("Speech request is already active")
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=16)
            self._inputs[request_id] = queue

            async def audio() -> AsyncIterator[bytes]:
                while (frame := await queue.get()) is not None:
                    yield frame

            stream = stream_transcription(
                audio(),
                language=params.get("language"),
                model=model,
                provider_preset=preset,
                request_kwargs=request_kwargs,
                usage_binding=usage_binding,
            )
        else:
            raise ValueError("Unsupported speech operation")
        try:
            async with aclosing(stream):
                async for event in stream:
                    yield event
        finally:
            self._inputs.pop(request_id, None)
            self._committed.discard(request_id)
