"""Speech-to-text transcription endpoint for the web microphone flow.

Mirrors the existing channel inbound STT path (``cognis/channels/inbound.py``)
but exposes it as a regular HTTP endpoint. Accepts either a multipart audio
upload or a reference to an already-uploaded artifact.

The audio bytes are normalized via ``cognis.audio.preprocessing`` (passthrough
when the MIME is supported, ffmpeg-based WAV transcode otherwise) and routed
through ``LLMProvider.transcribe()``, which honors the configured executor
location for the speech-to-text route.
"""

from __future__ import annotations

import mimetypes
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import SttTranscribeResponse
from cognis.audio.preprocessing import normalize_audio_mime_type
from cognis.logging import get_logger
from cognis.store.queries import get_artifact_record

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/stt", tags=["stt"])


_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB matches OpenAI's whisper limit


def _is_audio_mime(mime_type: str | None) -> bool:
    return normalize_audio_mime_type(mime_type).startswith("audio/")


def _friendly_transcription_error(exc: Exception) -> str | None:
    message = str(exc).lower()
    expected_markers = (
        "400 bad request",
        "corrupted",
        "unsupported",
        "could not be converted",
        "empty audio",
        "invalid audio",
        "audio file might be",
    )
    if any(marker in message for marker in expected_markers):
        return (
            "I couldn't transcribe that recording. It may be too short, silent, "
            "corrupted, or in an unsupported format."
        )
    return None


@router.post("/transcribe", response_model=SttTranscribeResponse)
async def transcribe_stt(
    request: Request,
    file: Annotated[UploadFile | None, File()] = None,
    artifact_id: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    prompt: Annotated[str | None, Form()] = None,
) -> SttTranscribeResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)

    audio_bytes: bytes
    mime_type: str
    filename: str

    if file is not None:
        audio_bytes = await file.read()
        if len(audio_bytes) == 0:
            raise api_exception(400, "validation_error", "Empty audio file")
        if len(audio_bytes) > _MAX_AUDIO_BYTES:
            raise api_exception(
                413,
                "payload_too_large",
                "Audio exceeds 25MB transcription limit",
            )
        filename = file.filename or "voice-input.webm"
        guessed = mimetypes.guess_type(filename)[0]
        mime_type = normalize_audio_mime_type(
            file.content_type
            if _is_audio_mime(file.content_type)
            else (guessed if _is_audio_mime(guessed) else "application/octet-stream")
        )
    elif artifact_id is not None:
        artifact_store = request.app.state.artifact_store
        async with request.app.state.session_factory() as session:
            row = await get_artifact_record(session, artifact_id)
        if row is None or row.status == "deleted":
            raise api_exception(404, "not_found", "Artifact not found")
        if (
            row.owner_email
            and row.owner_email != user.email
            and getattr(user, "role", "") != "admin"
        ):
            raise api_exception(404, "not_found", "Artifact not found")
        if not _is_audio_mime(row.mime_type):
            raise api_exception(400, "validation_error", "Artifact is not an audio file")
        audio_bytes, content_type = await artifact_store.async_load(
            row.namespace, row.object_id, row.filename
        )
        mime_type = normalize_audio_mime_type(content_type or row.mime_type)
        filename = row.filename
    else:
        raise api_exception(400, "validation_error", "Provide either 'file' or 'artifact_id'")

    llm = request.app.state.providers.llm
    try:
        result = await llm.transcribe(
            audio_bytes,
            mime_type=mime_type,
            filename=filename,
            language=language,
            prompt=prompt,
        )
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        friendly = _friendly_transcription_error(exc)
        if friendly is not None:
            logger.warning(
                "STT transcribe rejected invalid audio",
                extra={"extra_data": {"error_class": exc.__class__.__name__}},
            )
            raise api_exception(400, "stt_invalid_audio", friendly) from exc
        logger.exception("STT transcribe failed")
        raise api_exception(502, "stt_failed", "Transcription failed") from exc

    return SttTranscribeResponse(
        text=result.text,
        language=result.language,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )
