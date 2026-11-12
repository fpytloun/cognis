"""Bounded audio admission for exact-revision public document capabilities."""

from __future__ import annotations

from hashlib import sha256

from cognis.core.audio_jobs import AudioJobService
from cognis.core.speech_models import AudioJobCreate, AudioJobView
from cognis.store.models import AudioJobRow


def public_audio_request(ref: str, digest: str, payload: AudioJobCreate) -> AudioJobCreate:
    """Deduplicate all visitors across shares into at most four paid source plans."""
    if payload.source_ref != ref or payload.voice is not None or payload.target_minutes is not None:
        raise ValueError("Public audio accepts only the shared source and default voice.")
    if payload.mode == "narration" and payload.length is not None:
        raise ValueError("Narration does not accept a podcast length.")
    length = (payload.length or "default") if payload.mode == "podcast" else None
    key = sha256(f"{ref}:{digest}:{payload.mode}:{length}".encode()).hexdigest()
    return AudioJobCreate(
        content_ref=ref,
        mode=payload.mode,
        length=length,
        idempotency_key=f"public-audio:{key}",
    )


async def public_audio_job(
    service: AudioJobService, owner: str, ref: str, job_id: str
) -> AudioJobView:
    """Check the pinned source before signing any job output URLs."""
    async with service.sessions() as db:
        row = await db.get(AudioJobRow, job_id)
    if row is None or row.owner_email != owner or row.deliverable_id != ref:
        raise PermissionError("Audio job not found")
    return await service.view(owner, job_id)
