"""Portable ElevenLabs speech transport for controller and executor inference."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from cognis.models.config import SpeechToTextResult, TextToSpeechResult
from cognis.models.speech import (
    MAX_SPEECH_FRAME_BYTES,
    MAX_SPEECH_INPUT_BYTES,
    speech_streaming_capabilities,
)
from cognis.providers.llm.hosted import api_endpoint
from cognis.providers.llm.speech_streaming import stream_transcription
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_http import observed_http

BASE_URL = "https://api.elevenlabs.io"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"
FORMATS = {
    "mp3": ("mp3_44100_128", "audio/mpeg"),
    "pcm": ("pcm_44100", "audio/pcm"),
}


def headers(kwargs: dict[str, Any]) -> dict[str, str]:
    """Require explicit credentials; never fall back to another provider's key."""
    key = kwargs.get("api_key")
    if not isinstance(key, str) or not key:
        raise ValueError("ElevenLabs credentials unavailable")
    return {**(kwargs.get("extra_headers") or {}), "xi-api-key": key}


def base(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("api_base") or kwargs.get("base_url") or BASE_URL)


async def discover_voices(*, base_url: str, api_key: str) -> list[dict[str, str]]:
    """List account voices without generating billable audio."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            api_endpoint(base_url or BASE_URL, "voices"),
            headers={"xi-api-key": api_key},
        )
        response.raise_for_status()
        return [
            {"voice_id": voice["voice_id"], "name": voice.get("name", voice["voice_id"])}
            for voice in response.json().get("voices", [])
            if isinstance(voice.get("voice_id"), str)
        ]


async def synthesize(
    *,
    text: str,
    model: str,
    voice: str,
    response_format: str,
    speed: float,
    request_kwargs: dict[str, Any],
    usage_binding: UsageBinding | None = None,
) -> TextToSpeechResult:
    """Issue exactly one native request, with provider-supported options only."""
    if response_format not in FORMATS:
        raise ValueError("ElevenLabs synthesis supports mp3 or pcm in Cognis")
    if not 0.7 <= speed <= 1.2:
        raise ValueError("ElevenLabs speed must be between 0.7 and 1.2")
    output_format, content_type = FORMATS[response_format]
    async with httpx.AsyncClient(timeout=request_kwargs.get("timeout", 120)) as client:
        response = await observed_http(
            lambda: client.post(
                api_endpoint(base(request_kwargs), f"text-to-speech/{quote(voice, safe='')}"),
                headers=headers(request_kwargs),
                params={"output_format": output_format},
                json={
                    "text": text,
                    "model_id": model.removeprefix("elevenlabs/"),
                    "voice_settings": {"speed": speed},
                },
            ),
            binding=usage_binding,
            parameters={"input_characters": len(text)},
        )
        response.raise_for_status()
    if not response.content:
        raise ValueError("ElevenLabs returned empty audio")
    return TextToSpeechResult(
        audio_bytes=response.content,
        content_type=content_type,
        model=model,
        voice=voice,
    )


async def transcribe(
    *,
    audio_bytes: bytes,
    mime_type: str,
    filename: str,
    model: str,
    language: str | None,
    prompt: str | None,
    request_kwargs: dict[str, Any],
    usage_binding: UsageBinding | None = None,
) -> SpeechToTextResult:
    """Use the selected Scribe model's native protocol without substitution."""
    if prompt:
        raise ValueError("ElevenLabs Scribe does not support a transcription prompt")
    if speech_streaming_capabilities("elevenlabs", model).stt:
        headers(request_kwargs)
        async with asyncio.timeout(float(request_kwargs.get("timeout") or 120)):
            pcm = await _recording_pcm(audio_bytes)

            async def frames() -> AsyncIterator[bytes]:
                for offset in range(0, len(pcm), MAX_SPEECH_FRAME_BYTES):
                    yield pcm[offset : offset + MAX_SPEECH_FRAME_BYTES]

            text = None
            complete = False
            async for event in stream_transcription(
                audio=frames(),
                model=model,
                provider_preset="elevenlabs",
                request_kwargs=request_kwargs,
                language=language,
                usage_binding=usage_binding,
            ):
                if event.type == "transcript":
                    text = event.text
                elif event.type == "done":
                    complete = True
            if not complete or text is None:
                raise RuntimeError("ElevenLabs returned an incomplete transcript")
            return SpeechToTextResult(text=text, language=language, model=model)
    data = {"model_id": model.removeprefix("elevenlabs/"), "tag_audio_events": "false"}
    if language:
        data["language_code"] = language
    async with httpx.AsyncClient(timeout=request_kwargs.get("timeout", 120)) as client:
        response = await observed_http(
            lambda: client.post(
                api_endpoint(base(request_kwargs), "speech-to-text"),
                headers=headers(request_kwargs),
                data=data,
                files={"file": (filename, audio_bytes, mime_type)},
            ),
            binding=usage_binding,
            parameters={},
        )
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("text"), str):
        raise ValueError("ElevenLabs returned no transcript")
    return SpeechToTextResult(
        text=payload["text"],
        language=payload.get("language_code"),
        model=model,
    )


async def _recording_pcm(content: bytes) -> bytes:
    """Bound recorded audio before dispatching it to Scribe's PCM-only protocol."""
    if not content or len(content) > MAX_SPEECH_INPUT_BYTES:
        raise ValueError("Recording is empty or exceeds the speech input limit")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("Recorded realtime transcription requires FFmpeg on the inference host")
    with tempfile.TemporaryDirectory(prefix="cognis_scribe_") as directory:
        path = Path(directory) / "recording"
        await asyncio.to_thread(path.write_bytes, content)
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            "aac,amr,flac,matroska,webm,mov,mp3,ogg,wav",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            f"aresample=16000,atrim=end_sample={MAX_SPEECH_INPUT_BYTES // 2 + 1}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            pcm, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode != 0 or not pcm:
            raise ValueError("Recording could not be decoded for realtime transcription")
        if len(pcm) > MAX_SPEECH_INPUT_BYTES:
            raise ValueError("Decoded recording exceeds the speech input limit")
        return pcm
