"""Validate live PCM and finalize one complete replayable speech artifact."""

from __future__ import annotations

import asyncio
import base64
import shutil
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any

from cognis.models.config import TextToSpeechResult
from cognis.models.speech import MAX_SPEECH_FRAME_BYTES, PCM_SAMPLE_RATE, SpeechStreamEvent

_FORMATS = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


def can_finalize_speech(format: str) -> bool:
    """Preflight encoding before dispatching a paid request."""
    return format in _FORMATS and (format == "pcm" or shutil.which("ffmpeg") is not None)


async def render_speech_stream(
    stream: AsyncGenerator[dict[str, Any], None],
    *,
    format: str,
    model: str,
    voice: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> TextToSpeechResult:
    """Never return incomplete, reordered, oversized, or falsely completed audio."""
    if not can_finalize_speech(format):
        raise ValueError("Speech streaming encoding is unavailable")
    pcm = bytearray()
    started = complete = False
    async with aclosing(stream):
        async for raw in stream:
            event = SpeechStreamEvent.model_validate(raw)
            if complete or event.sample_rate != PCM_SAMPLE_RATE:
                raise ValueError("Invalid speech stream lifecycle or sample rate")
            if event.type == "start":
                if started:
                    raise ValueError("Speech stream started twice")
                started = True
            elif event.type == "audio":
                if not started or event.sample_offset != len(pcm) // 2:
                    raise ValueError("Speech audio is out of order")
                if len(event.audio_base64) > MAX_SPEECH_FRAME_BYTES * 4 // 3 + 4:
                    raise ValueError("Speech audio frame is too large")
                frame = base64.b64decode(event.audio_base64, validate=True)
                if not frame or len(frame) % 2 or len(frame) > MAX_SPEECH_FRAME_BYTES:
                    raise ValueError("Invalid speech audio frame")
                pcm.extend(frame)
                if len(pcm) > 25 * 1024 * 1024:
                    raise ValueError("Speech audio exceeds the part limit")
            elif event.type == "done":
                if not started or not pcm or event.sample_offset != len(pcm) // 2:
                    raise ValueError("Speech stream completion does not match its audio")
                complete = True
            else:
                raise ValueError("Unexpected synthesis event")
            if on_event:
                await on_event(event.model_dump(mode="json", exclude_none=True))
    if not complete:
        raise RuntimeError("Speech stream ended before provider completion")
    audio = bytes(pcm)
    if format != "pcm":
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(PCM_SAMPLE_RATE),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            "adts" if format == "aac" else format,
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(30):
                audio, _ = await process.communicate(audio)
            if process.returncode != 0 or not audio:
                raise RuntimeError("Speech audio encoding failed")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    return TextToSpeechResult(
        audio_bytes=audio,
        content_type=_FORMATS[format],
        model=model,
        voice=voice,
        duration_seconds=len(pcm) / (2 * PCM_SAMPLE_RATE),
    )
