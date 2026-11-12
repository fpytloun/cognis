"""Durable, revision-bound narration with fenced publication and no paid replay."""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from cognis.core.artifact_revisions import ContentActor
from cognis.core.audio_planning import podcast_length_estimates
from cognis.core.audio_sources import AudioSource, load_audio_source, source_speech_binding
from cognis.core.speech import AudioNarrativeError, SpeechService, SpeechUnavailable, speech_chunks
from cognis.core.speech_models import (
    AudioJobCreate,
    AudioJobView,
    PodcastLengthEstimate,
    SpeechRequest,
)
from cognis.logging import get_logger
from cognis.providers.llm.errors import classify_llm_exception
from cognis.store.models import AudioJobRow
from cognis.store.queries import get_artifact_record

logger = get_logger(__name__)
ACTIVE = ("scripting", "synthesizing")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")


def audio_failure_code(exc: Exception) -> str:
    """Persist actionable categories, never provider text or submitted content."""
    if isinstance(exc, AudioNarrativeError):
        return exc.code
    if isinstance(exc, SpeechUnavailable):
        return "speech_unavailable"
    payload = classify_llm_exception(exc)
    code = payload.get("code")
    code = code if isinstance(code, str) else None
    if code in {"TypeError", "AttributeError", "NameError"}:
        return "generation_internal_error"
    if code in {"invalid_api_key", "missing_permissions", "unauthorized"}:
        return "provider_access_denied"
    cause: BaseException | None = exc
    for _ in range(8):
        if cause is None:
            break
        status = getattr(cause, "status_code", None)
        if status is None:
            status = getattr(getattr(cause, "response", None), "status_code", None)
        if status == 401 or status == 403:
            return "provider_access_denied"
        cause = cause.__cause__
    return {
        "quota_exhausted": "provider_quota_exhausted",
        "rate_limit": "provider_rate_limited",
        "invalid_request": "provider_request_rejected",
        "content_policy": "provider_request_rejected",
        "connection": "provider_unavailable",
        "provider_5xx": "provider_unavailable",
        "idle_timeout_raw": "provider_unavailable",
        "idle_timeout_activity": "provider_unavailable",
        "idle_timeout_reasoning": "provider_unavailable",
        "protocol": "speech_stream_incomplete",
    }.get(str(payload.get("category")), "generation_failed")


class AudioJobService:
    """DB CAS owns status; the maintenance lease owns one worker invocation.

    No DB transaction spans storage or inference. Cancellation fences every
    progress publication. Lost workers become interrupted, never requeued:
    provider requests may have been billed, so retry requires explicit intent.
    """

    def __init__(self, speech: SpeechService) -> None:
        self.speech = speech
        self.sessions = speech.sessions

    async def source(self, owner: str, deliverable_id: str, *, load: bool = False) -> AudioSource:
        """Authorize the exact revision before leaving the transaction for storage."""
        return await load_audio_source(self.speech, owner, deliverable_id, load=load)

    async def estimates(self, owner: str, ref: str) -> list[PodcastLengthEstimate]:
        """Authorize before bounded source hydration; never call a model or discover voices."""
        source = await self.source(owner, ref, load=True)
        if not source.supported:
            raise ValueError("This artifact has no readable text for audio generation.")
        return podcast_length_estimates(source.spoken_text())

    async def create(self, owner: str, request: AudioJobCreate) -> AudioJobView:
        """Idempotent request admission; matching keys cannot alter source or options."""
        source = await self.source(owner, request.source_ref)
        if not source.supported:
            raise ValueError("This artifact has no readable text for audio generation.")
        length = (
            request.length or "default"
            if request.mode == "podcast" and request.target_minutes is None
            else None
        )

        def matches(existing: AudioJobRow) -> bool:
            # Before relative plans, omitting both fields meant six minutes.
            # Reconcile that accepted request after an upgrade instead of losing
            # an uncertain paid intent; explicit new length choices still differ.
            legacy_default = (
                request.mode == "podcast"
                and request.length is None
                and request.target_minutes is None
                and existing.length_preference is None
                and existing.target_minutes == 6
            )
            return (
                existing.deliverable_id == request.source_ref
                and existing.mode == request.mode
                and existing.voice == request.voice
                and (existing.length_preference == length or legacy_default)
                and (length is not None or existing.target_minutes == (request.target_minutes or 6))
            )

        # Reconcile accepted intent before storage access: a lost response must not
        # regenerate a plan, change a persisted budget, or require another source GET.
        async with self.sessions() as session:
            existing = await session.scalar(
                select(AudioJobRow).where(
                    AudioJobRow.owner_email == owner,
                    AudioJobRow.idempotency_key == request.idempotency_key,
                )
            )
        if existing:
            if not matches(existing):
                raise ValueError("Idempotency key already belongs to different audio options")
            return await self.view(owner, existing.job_id)
        target_words = None
        target_minutes = request.target_minutes or 6
        if length is not None:
            estimates = await self.estimates(owner, request.source_ref)
            plan = next(plan for plan in estimates if plan.length == length)
            target_words = plan.target_words
            target_minutes = max(2, min(20, math.ceil(target_words / 140)))
        row = AudioJobRow(
            job_id="audio_" + uuid.uuid4().hex,
            owner_email=owner,
            deliverable_id=request.source_ref,
            mode=request.mode,
            voice=request.voice,
            target_minutes=target_minutes,
            length_preference=length,
            target_words=target_words,
            idempotency_key=request.idempotency_key,
            status="queued",
            total_parts=0,
            part_artifact_ids=[],
        )
        try:
            async with self.sessions() as session:
                session.add(row)
                await session.commit()
        except IntegrityError:
            async with self.sessions() as session:
                row = await session.scalar(
                    select(AudioJobRow).where(
                        AudioJobRow.owner_email == owner,
                        AudioJobRow.idempotency_key == request.idempotency_key,
                    )
                )
            if row is None:
                raise
            if not matches(row):
                raise ValueError(
                    "Idempotency key already belongs to different audio options"
                ) from None
        return await self.view(owner, row.job_id)

    async def view(self, owner: str, job_id: str) -> AudioJobView:
        """Require both job ownership and current source access before signing outputs."""
        async with self.sessions() as session:
            row = await session.get(AudioJobRow, job_id)
        if row is None or row.owner_email != owner:
            raise PermissionError("Audio job not found")
        await self.source(owner, row.deliverable_id)
        parts = []
        script_url = None
        async with self.sessions() as session:
            records = [
                await get_artifact_record(session, artifact_id)
                for artifact_id in row.part_artifact_ids
            ]
            script = (
                await get_artifact_record(session, row.script_artifact_id)
                if row.script_artifact_id
                else None
            )
        for record in records:
            if record and record.owner_email == owner and record.status != "deleted":
                parts.append(await self.speech.part(record))
        if script and script.owner_email == owner and script.status != "deleted":
            script_url = await self.speech.artifacts.async_get_public_url(
                script.namespace, script.object_id, script.filename
            )
        return AudioJobView(
            job_id=row.job_id,
            deliverable_id=row.deliverable_id,
            content_ref=row.deliverable_id,
            mode=row.mode,
            length=row.length_preference,
            target_words=row.target_words,
            status=row.status,
            total_parts=row.total_parts,
            completed_parts=len(row.part_artifact_ids),
            parts=parts,
            script_artifact_id=row.script_artifact_id,
            script_url=script_url,
            error_code=row.error_code,
            created_at=row.created_at.isoformat(),
            warnings=row.warnings or [],
        )

    async def latest(self, owner: str, deliverable_id: str) -> list[AudioJobView]:
        """Find recent work for this revision after refresh or navigation."""
        await self.source(owner, deliverable_id)
        async with self.sessions() as session:
            ids = list(
                (
                    await session.scalars(
                        select(AudioJobRow.job_id)
                        .where(
                            AudioJobRow.owner_email == owner,
                            AudioJobRow.deliverable_id == deliverable_id,
                        )
                        .order_by(AudioJobRow.created_at.desc())
                        .limit(10)
                    )
                ).all()
            )
        return [await self.view(owner, job_id) for job_id in ids]

    async def cancel(self, owner: str, job_id: str) -> AudioJobView:
        """Persist cancellation even if another replica owns the active request."""
        await self.view(owner, job_id)
        async with self.sessions() as session:
            await session.execute(
                update(AudioJobRow)
                .where(
                    AudioJobRow.job_id == job_id,
                    AudioJobRow.owner_email == owner,
                    AudioJobRow.status.in_(("queued", *ACTIVE)),
                )
                .values(status="cancelled", updated_at=datetime.now(UTC))
            )
            await session.commit()
        return await self.view(owner, job_id)

    async def progress(self, job_id: str, **values: Any) -> bool:
        """A terminal row cannot be revived by late inference/storage completion."""
        async with self.sessions() as session:
            result = await session.execute(
                update(AudioJobRow)
                .where(AudioJobRow.job_id == job_id, AudioJobRow.status.in_(ACTIVE))
                .values(**values, updated_at=datetime.now(UTC))
            )
            await session.commit()
        return bool(result.rowcount == 1)

    async def tick(self) -> None:
        """Claim at most one job; another worker cannot claim the same queued row."""
        async with self.sessions() as session:
            await session.execute(
                update(AudioJobRow)
                .where(
                    AudioJobRow.status.in_(ACTIVE),
                    AudioJobRow.updated_at < datetime.now(UTC) - timedelta(minutes=10),
                )
                .values(
                    status="interrupted",
                    error_code="worker_interrupted",
                    updated_at=datetime.now(UTC),
                )
            )
            job_id = await session.scalar(
                select(AudioJobRow.job_id)
                .where(AudioJobRow.status == "queued")
                .order_by(AudioJobRow.created_at)
                .limit(1)
            )
            if job_id is None:
                await session.commit()
                return
            claimed = await session.execute(
                update(AudioJobRow)
                .where(
                    AudioJobRow.job_id == job_id,
                    AudioJobRow.status == "queued",
                )
                .values(status="scripting", updated_at=datetime.now(UTC))
            )
            await session.commit()
            if claimed.rowcount != 1:
                return
            row = await session.get(AudioJobRow, job_id)
        try:
            await self.run(row)
        except asyncio.CancelledError:
            await self.progress(job_id, status="interrupted", error_code="worker_interrupted")
            raise
        except PermissionError:
            await self.progress(job_id, status="failed", error_code="source_unavailable")
        except Exception as exc:
            code = audio_failure_code(exc)
            logger.warning(
                "Audio generation failed",
                extra={
                    "extra_data": {
                        "job_id": job_id,
                        "error_class": type(exc).__name__,
                        "error_code": code,
                    }
                },
            )
            await self.progress(job_id, status="failed", error_code=code)

    async def run(self, row: AudioJobRow) -> None:
        """Generate source-grounded prose and immutable ordered MP3 parts."""
        actor = ContentActor(row.owner_email, None, None, None, human=True)
        source = await self.source(row.owner_email, row.deliverable_id, load=True)
        text = source.spoken_text()
        if not text.strip() or len(text) > 120_000:
            raise ValueError("Deliverable must contain between 1 and 120000 text characters")
        binding = await source_speech_binding(self.speech, row.owner_email, source, row.voice)
        model, provider, voice = binding.model, binding.provider, binding.voice
        if not await self.progress(row.job_id):
            return
        if row.mode == "podcast":
            text, script_id = await self.speech.adapted_script(
                actor,
                text,
                podcast=True,
                minutes=row.target_minutes,
                target_words=row.target_words,
                source_id=row.deliverable_id,
            )
        else:
            script_id = await self.speech.save_script(actor, text)
        chunks = speech_chunks(text, first_limit=240)
        if not chunks:
            raise ValueError("No narratable content")
        if not await self.progress(
            row.job_id,
            script_artifact_id=script_id,
            total_parts=len(chunks),
            status="synthesizing",
            model=model,
            provider_id=provider,
            warnings=list(binding.warnings),
        ):
            return
        parts = []
        request = SpeechRequest(text=text, voice=voice)
        for chunk in chunks:
            # Revalidate access before each paid chunk; no silent source/head changes.
            await self.source(row.owner_email, row.deliverable_id)
            if not await self.progress(row.job_id):
                return
            part, _ = await self.speech.synthesize_part(
                actor,
                request,
                chunk,
                model=model,
                provider=provider,
                voice=voice,
                durable=True,
                streaming=binding.streaming,
            )
            parts.append(part.artifact_id)
            if not await self.progress(row.job_id, part_artifact_ids=list(parts)):
                return
        await self.progress(row.job_id, status="completed")
