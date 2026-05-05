"""Text-to-speech synthesis endpoint with artifact-store caching.

The endpoint resolves the configured TTS provider via ``model_routing``,
synthesizes audio through ``LLMProvider.synthesize()`` (which routes to a
remote executor when the provider is configured with ``location='executor'``),
caches the result in the artifact store, and returns a signed URL.

The cache key is ``(message_id, voice, model)``. When ``message_id`` is
omitted (e.g. one-off text), a fresh artifact is written without a cache
metadata row — subsequent calls will re-synthesize.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import TtsSynthesizeRequest, TtsSynthesizeResponse
from cognis.core.voice_resolution import (
    provider_default_voice_from_config,
    resolve_voice,
)
from cognis.logging import get_logger
from cognis.models.artifact import ArtifactKind
from cognis.store.queries import (
    create_artifact_record,
    get_agent,
    get_artifact_record,
    get_setting_value,
    get_tts_cache_entry,
    insert_tts_cache_entry,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/tts", tags=["tts"])


_TTS_FORMAT_TO_EXTENSION: dict[str, str] = {
    "mp3": ".mp3",
    "opus": ".opus",
    "aac": ".aac",
    "flac": ".flac",
    "wav": ".wav",
    "pcm": ".pcm",
}


def _tts_cache_object_id(*, message_id: str, voice: str, model: str) -> str:
    digest = hashlib.sha256(f"{message_id}|{voice}|{model}".encode()).hexdigest()[:32]
    return f"tts_{digest}"


@router.post("/synthesize", response_model=TtsSynthesizeResponse)
async def synthesize_tts(
    request: Request,
    payload: TtsSynthesizeRequest,
) -> TtsSynthesizeResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)

    text = (payload.text or "").strip()
    if not text:
        raise api_exception(400, "validation_error", "text is required")
    if len(text) > 8192:
        raise api_exception(
            400,
            "validation_error",
            "text exceeds maximum TTS length (8192 characters)",
        )

    # Master kill-switch
    async with request.app.state.session_factory() as session:
        if not bool(await get_setting_value(session, "tts.enabled", True)):
            raise api_exception(503, "tts_disabled", "TTS is disabled")
        system_default_voice = await get_setting_value(session, "tts.default_voice", None)

    # Resolve agent voice (if agent_id provided).
    agent_voice: str | None = None
    if payload.agent_id:
        async with request.app.state.session_factory() as session:
            agent_row = await get_agent(session, payload.agent_id)
            if agent_row is not None and isinstance(agent_row.llm_config, dict):
                voice = agent_row.llm_config.get("voice")
                if isinstance(voice, str) and voice.strip():
                    agent_voice = voice.strip()

    llm = request.app.state.providers.llm
    # Resolve TTS model and provider through the existing routing chain.
    # ``resolve_model_target`` returns ``(model_id, provider_id | None)`` —
    # the second element is the provider id string, not a row.
    try:
        resolved_model, resolved_provider_id = await llm.resolve_model_target(
            None, task_type="text_to_speech"
        )
    except Exception as exc:
        raise api_exception(
            503,
            "tts_unconfigured",
            f"No TTS model is configured. Set the text_to_speech route in settings. ({exc})",
        ) from exc
    if resolved_provider_id is None:
        raise api_exception(
            503,
            "tts_unconfigured",
            "TTS routing has no provider. Configure a text-to-speech model in settings.",
        )

    provider_default_voice: str | None = None
    async with request.app.state.session_factory() as session:
        from cognis.store.models import LLMProvider as LLMProviderRow

        provider_row = await session.get(LLMProviderRow, resolved_provider_id)
        if provider_row is not None and isinstance(provider_row.config, dict):
            provider_default_voice = provider_default_voice_from_config(provider_row.config)

    voice = resolve_voice(
        explicit=payload.voice,
        agent_voice=agent_voice,
        provider_default_voice=provider_default_voice,
        system_default_voice=(
            system_default_voice if isinstance(system_default_voice, str) else None
        ),
    )
    response_format = (payload.format or "mp3").strip().lower()
    if response_format not in _TTS_FORMAT_TO_EXTENSION:
        response_format = "mp3"

    # Cache lookup
    artifact_store = request.app.state.artifact_store
    if payload.message_id:
        async with request.app.state.session_factory() as session:
            cached = await get_tts_cache_entry(
                session,
                message_id=payload.message_id,
                voice=voice,
                model=resolved_model,
            )
        if cached is not None:
            # Verify the artifact still exists (defensive).
            exists = await artifact_store.async_exists(
                "tts", cached.artifact_id, cached.artifact_filename
            )
            if exists:
                signed_url = await artifact_store.async_get_public_url(
                    "tts", cached.artifact_id, cached.artifact_filename
                )
                return TtsSynthesizeResponse(
                    audio_url=signed_url,
                    content_type=cached.content_type,
                    duration_seconds=cached.duration_seconds,
                    voice=cached.voice,
                    model=cached.model,
                    cached=True,
                )

    # Cache miss — synthesize.
    try:
        result = await llm.synthesize(
            text,
            voice=voice,
            response_format=response_format,
            speed=payload.speed,
            low_latency=payload.low_latency,
        )
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        logger.exception("TTS synthesize failed")
        raise api_exception(502, "tts_failed", f"TTS synthesis failed: {exc}") from exc

    extension = _TTS_FORMAT_TO_EXTENSION.get(response_format, ".bin")
    if payload.message_id:
        artifact_id = _tts_cache_object_id(
            message_id=payload.message_id, voice=voice, model=result.model
        )
    else:
        artifact_id = artifact_store.generate_id("tts")
    filename = f"speech{extension}"

    await artifact_store.async_save(
        "tts",
        artifact_id,
        filename,
        result.audio_bytes,
        result.content_type,
        owner_email=user.email,
    )

    record_status = "active" if payload.message_id else "temporary"
    record_expires_at = None if payload.message_id else datetime.now(UTC) + timedelta(hours=1)

    async with request.app.state.session_factory() as session:
        if payload.message_id:
            await insert_tts_cache_entry(
                session,
                message_id=payload.message_id,
                voice=voice,
                model=result.model,
                artifact_id=artifact_id,
                artifact_filename=filename,
                content_type=result.content_type,
                owner_email=user.email,
                duration_seconds=result.duration_seconds,
                size_bytes=len(result.audio_bytes),
            )
        # The artifact_id is deterministic when message_id is set, so a row
        # for it may already exist (e.g. an earlier successful synthesize
        # whose tts_cache row has since been pruned, or a partial write
        # where the cache row failed but the artifact row committed).
        # Update the existing row instead of inserting to avoid a
        # UniqueViolation on artifacts_pkey.
        existing_record = await get_artifact_record(session, artifact_id)
        if existing_record is None:
            await create_artifact_record(
                session,
                artifact_id=artifact_id,
                namespace="tts",
                object_id=artifact_id,
                filename=filename,
                owner_email=user.email,
                purpose="tts",
                kind=ArtifactKind.AUDIO.value,
                mime_type=result.content_type,
                size_bytes=len(result.audio_bytes),
                status=record_status,
                expires_at=record_expires_at,
            )
        else:
            existing_record.namespace = "tts"
            existing_record.object_id = artifact_id
            existing_record.filename = filename
            existing_record.owner_email = user.email
            existing_record.purpose = "tts"
            existing_record.kind = ArtifactKind.AUDIO.value
            existing_record.mime_type = result.content_type
            existing_record.size_bytes = len(result.audio_bytes)
            existing_record.status = record_status
            existing_record.expires_at = record_expires_at
            existing_record.deleted_at = None
        await session.commit()

    signed_url = await artifact_store.async_get_public_url("tts", artifact_id, filename)
    return TtsSynthesizeResponse(
        audio_url=signed_url,
        content_type=result.content_type,
        duration_seconds=result.duration_seconds,
        voice=voice,
        model=result.model,
        cached=False,
    )
