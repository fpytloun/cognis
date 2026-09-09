"""Executor-local text-to-speech transport helpers."""

from __future__ import annotations

import inspect
import logging
from typing import Any

import httpx
import litellm

from cognis.models.config import TextToSpeechResult

logger = logging.getLogger(__name__)

_TTS_FORMAT_TO_CONTENT_TYPE: dict[str, str] = {
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


async def run_synthesize_local(
    *,
    text: str,
    voice: str,
    wire_model: str,
    response_format: str,
    speed: float,
    request_kwargs: dict[str, Any],
    resolved_model: str,
    provider_preset: str,
    prefer_direct_http: bool = False,
) -> TextToSpeechResult:
    """Run text-to-speech without importing the controller provider stack."""

    content_type = _TTS_FORMAT_TO_CONTENT_TYPE.get(
        response_format.strip().lower(),
        "application/octet-stream",
    )
    direct_first = prefer_direct_http and provider_preset in {
        "openai",
        "openai_compatible",
        "litellm_proxy",
    }
    aspeech = getattr(litellm, "aspeech", None)
    if callable(aspeech) and not direct_first:
        try:
            result = await aspeech(
                model=wire_model,
                input=text,
                voice=voice,
                response_format=response_format,
                speed=speed,
                api_key=request_kwargs.get("api_key"),
                api_base=request_kwargs.get("api_base") or request_kwargs.get("base_url"),
                timeout=request_kwargs.get("timeout", 120),
            )
            audio_bytes = await _extract_tts_bytes(result)
            if audio_bytes:
                return TextToSpeechResult(
                    audio_bytes=audio_bytes,
                    content_type=content_type,
                    model=resolved_model,
                    voice=voice,
                    duration_seconds=None,
                )
        except Exception:
            logger.debug("litellm.aspeech failed; falling back to direct HTTP", exc_info=True)

    api_base = request_kwargs.get("api_base") or request_kwargs.get("base_url")
    if not isinstance(api_base, str) or not api_base:
        api_base = "https://api.openai.com"
    api_key = request_kwargs.get("api_key")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if isinstance(api_key, str) and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    extra_headers = request_kwargs.get("extra_headers")
    if isinstance(extra_headers, dict):
        headers.update({str(key): str(value) for key, value in extra_headers.items()})

    timeout = request_kwargs.get("timeout", 120)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{api_base.rstrip('/')}/v1/audio/speech",
                headers=headers,
                json={
                    "model": wire_model,
                    "input": text,
                    "voice": voice,
                    "response_format": response_format,
                    "speed": speed,
                },
            )
            response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Text-to-speech request failed: {str(exc)[:500]}") from exc

    audio_bytes = response.content
    if not audio_bytes:
        raise RuntimeError("Text-to-speech returned an empty audio payload")
    response_content_type = response.headers.get("content-type")
    if isinstance(response_content_type, str) and response_content_type.startswith("audio/"):
        content_type = response_content_type.split(";", 1)[0].strip()
    return TextToSpeechResult(
        audio_bytes=audio_bytes,
        content_type=content_type,
        model=resolved_model,
        voice=voice,
        duration_seconds=None,
    )


async def _extract_tts_bytes(result: Any) -> bytes:
    if isinstance(result, bytes | bytearray):
        return bytes(result)
    for attr in ("content", "audio_bytes"):
        value = getattr(result, attr, None)
        if isinstance(value, bytes | bytearray) and value:
            return bytes(value)
    read = getattr(result, "read", None)
    if callable(read):
        data = read()
        if inspect.isawaitable(data):
            data = await data
        if isinstance(data, bytes | bytearray):
            return bytes(data)
    iter_bytes = getattr(result, "iter_bytes", None)
    if callable(iter_bytes):
        chunks = [bytes(chunk) for chunk in iter_bytes() if isinstance(chunk, bytes | bytearray)]
        if chunks:
            return b"".join(chunks)
    return b""
