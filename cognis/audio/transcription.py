"""Shared speech-to-text helpers for stored and inbound audio."""

from __future__ import annotations

import inspect
from typing import Any

from cognis.audio.preprocessing import (
    prepare_audio_for_stt,
    stt_supported_audio_mime_types,
)


async def resolve_stt_supported_mime_types(
    llm: Any,
    *,
    acting_user_email: str | None = None,
) -> list[str] | None:
    """Return the active STT model's accepted audio MIME types when discoverable."""

    resolver = getattr(llm, "resolve_model_target", None)
    if not callable(resolver):
        return None
    resolved = resolver(
        task_type="speech_to_text",
        acting_user_email=acting_user_email,
    )
    if inspect.isawaitable(resolved):
        resolved = await resolved
    if not isinstance(resolved, tuple) or not resolved:
        return None

    model = str(resolved[0])
    provider_id = resolved[1] if len(resolved) > 1 else None
    model_info = None
    info_getter = getattr(llm, "get_model_info", None)
    if callable(info_getter):
        info = info_getter(
            model,
            provider_id=provider_id,
            acting_user_email=acting_user_email,
        )
        model_info = await info if inspect.isawaitable(info) else info
    return stt_supported_audio_mime_types(model=model, model_info=model_info)


async def transcribe_audio_bytes(
    llm: Any,
    content: bytes,
    *,
    mime_type: str,
    filename: str,
    acting_user_email: str | None = None,
) -> str:
    """Prepare audio for the configured STT route and return its transcript."""

    supported_mime_types = await resolve_stt_supported_mime_types(
        llm,
        acting_user_email=acting_user_email,
    )
    prepared_content, prepared_mime, prepared_filename = await prepare_audio_for_stt(
        content,
        mime_type=mime_type,
        filename=filename,
        supported_mime_types=supported_mime_types,
    )
    result = await llm.transcribe(
        prepared_content,
        mime_type=prepared_mime,
        filename=prepared_filename,
        acting_user_email=acting_user_email,
    )
    return str(result.text or "").strip()
