"""Read-only document audio capabilities for authenticated and signed viewers."""

from __future__ import annotations

from cognis.core.audio_jobs import AudioJobService
from cognis.core.audio_sources import source_speech_binding
from cognis.core.speech_models import DocumentAudioContext


async def document_audio_context(
    service: AudioJobService, owner: str, ref: str, *, can_mutate: bool
) -> DocumentAudioContext:
    """Check source and TTS configuration without inference or voice discovery."""
    source = await service.source(owner, ref)
    detail = None
    can_generate = can_mutate and source.supported
    if not source.supported:
        detail = "This artifact has no readable text for narration or a podcast."
    elif can_generate:
        try:
            await source_speech_binding(service.speech, owner, source, None, validate_voice=False)
        except ValueError as exc:
            can_generate = False
            detail = str(exc)
    else:
        detail = "Your account has read-only access."
    return DocumentAudioContext(
        supported=source.supported,
        can_estimate=source.supported,
        can_generate=can_generate,
        can_cancel=can_mutate,
        detail=detail,
        jobs=await service.latest(owner, ref),
    )
