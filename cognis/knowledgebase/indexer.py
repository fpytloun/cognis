"""Background knowledgebase indexing worker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.knowledgebase.chunking import chunk_document
from cognis.knowledgebase.extraction import extract_artifact_bytes_bounded
from cognis.knowledgebase.vector import VectorPoint, sparse_vector_from_text
from cognis.logging import get_logger
from cognis.store.coordination import DatabaseLeaseStore, Lease
from cognis.store.models import (
    KnowledgebaseArtifactRow,
    KnowledgebaseChunkRow,
    KnowledgebaseIndexJobRow,
    KnowledgebaseRow,
)
from cognis.store.queries import (
    delete_knowledgebase_chunks,
    enqueue_knowledgebase_job,
    get_artifact_record,
    insert_knowledgebase_chunks,
    list_knowledgebase_job_claim_candidates,
)

logger = get_logger(__name__)
_JOB_LEASE_SECONDS = 300.0
_RESERVED_CHUNK_METADATA = {
    "owner_email",
    "knowledgebase_id",
    "kb_artifact_id",
    "generation",
    "artifact_id",
    "artifact_hash",
    "chunk_id",
    "filename",
    "mime_type",
    "kind",
    "purpose",
}


def _metadata_type_matches(value: Any, spec: dict[str, Any]) -> bool:
    raw_type = str(spec.get("type") or "string").lower()
    if raw_type in {"string", "keyword", "date", "datetime"}:
        return isinstance(value, str)
    if raw_type in {"integer"}:
        return isinstance(value, int) and not isinstance(value, bool)
    if raw_type in {"number", "float"}:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if raw_type == "boolean":
        return isinstance(value, bool)
    if raw_type in {"array", "string[]", "list[string]", "array[string]"}:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


def _merge_extracted_metadata(
    *,
    extracted: dict[str, Any],
    attached: dict[str, Any],
    metadata_schema: dict[str, Any],
) -> dict[str, Any]:
    if _RESERVED_CHUNK_METADATA.intersection(extracted):
        raise RuntimeError("frontmatter contains reserved metadata keys")
    fields = dict(metadata_schema.get("fields") or {})
    for key, value in extracted.items():
        spec = fields.get(key)
        if isinstance(spec, dict) and not _metadata_type_matches(value, spec):
            raise RuntimeError(f"frontmatter metadata type mismatch: {key}")
        if key in attached and attached[key] != value:
            raise RuntimeError(f"frontmatter conflicts with attachment metadata: {key}")
    return {**extracted, **attached}


_CLEANUP_JOB_TYPES = {"delete_artifact_index", "delete_stale_vectors"}


class _KnowledgebaseLeaseLost(RuntimeError):
    pass


class _KnowledgebaseGenerationStale(RuntimeError):
    pass


def _chunking_setting(settings: dict[str, Any], key: str) -> int | None:
    chunking = settings.get("chunking")
    if not isinstance(chunking, dict):
        return None
    value = chunking.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _resolve_chunking_settings(
    settings: dict[str, Any],
    *,
    default_max_chunks_per_artifact: int,
    default_target_tokens: int,
    default_overlap_tokens: int,
) -> tuple[int, int, int]:
    max_chunks = (
        _chunking_setting(settings, "max_chunks_per_artifact") or default_max_chunks_per_artifact
    )
    target_tokens = _chunking_setting(settings, "target_tokens") or default_target_tokens
    overlap_tokens = _chunking_setting(settings, "overlap_tokens")
    if overlap_tokens is None:
        return max_chunks, target_tokens, default_overlap_tokens
    return max_chunks, target_tokens, overlap_tokens


class KnowledgebaseIndexer:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        artifact_store: Any,
        llm: Any,
        vector_backend: Any,
        enabled: bool,
        poll_interval_seconds: float,
        max_artifact_size_bytes: int,
        max_chunks_per_artifact: int,
        chunk_target_tokens: int,
        chunk_overlap_tokens: int,
        embedding_batch_size: int,
        controller_owner_id: str = "local-controller",
    ) -> None:
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._llm = llm
        self._vector_backend = vector_backend
        self._enabled = enabled
        self._poll_interval_seconds = poll_interval_seconds
        self._max_artifact_size_bytes = max_artifact_size_bytes
        self._max_chunks_per_artifact = max_chunks_per_artifact
        self._chunk_target_tokens = max(1, chunk_target_tokens)
        self._chunk_overlap_tokens = max(0, chunk_overlap_tokens)
        self._embedding_batch_size = max(1, embedding_batch_size)
        self._controller_owner_id = controller_owner_id
        self._lease_store = DatabaseLeaseStore(session_factory)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="knowledgebase-indexer")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> bool:
        async with self._session_factory() as session:
            candidates = await list_knowledgebase_job_claim_candidates(session)
        claimed: tuple[KnowledgebaseIndexJobRow, Lease] | None = None
        for candidate in candidates:
            resource_id = candidate.kb_artifact_id or candidate.job_id
            lease = await self._lease_store.acquire(
                f"knowledgebase-index-attachment:{resource_id}",
                self._controller_owner_id,
                ttl_seconds=_JOB_LEASE_SECONDS,
            )
            if lease is not None:
                claimed = (candidate, lease)
                break
        if claimed is None:
            return False
        job, lease = claimed
        async with self._session_factory() as session:
            if not await self._lease_store.is_current_in_session(session, lease):
                await self._lease_store.release(lease)
                return False
            current = await session.get(KnowledgebaseIndexJobRow, job.job_id)
            if current is None or current.status not in {"queued", "running"}:
                await self._lease_store.release(lease)
                return False
            current.status = "running"
            current.started_at = current.started_at or datetime.now(UTC)
            current.updated_at = datetime.now(UTC)
            current.attempts += 1
            await session.commit()
            job = current

        lost = asyncio.Event()
        renewal = asyncio.create_task(self._renew_lease(lease, lost))
        try:
            if job.job_type == "delete_artifact_index":
                chunks_deleted = await self._delete_index(job, lease, lost)
                await self._finish_job(
                    job.job_id,
                    lease,
                    lost,
                    status="succeeded",
                    chunks_deleted=chunks_deleted,
                )
            elif job.job_type == "delete_stale_vectors":
                vectors_deleted = await self._delete_stale_vectors(job, lease, lost)
                await self._finish_job(
                    job.job_id,
                    lease,
                    lost,
                    status="succeeded",
                    chunks_deleted=vectors_deleted,
                )
            else:
                chunks_indexed = await self._index_artifact(job, lease, lost)
                await self._finish_job(
                    job.job_id,
                    lease,
                    lost,
                    status="succeeded",
                    chunks_indexed=chunks_indexed,
                )
        except _KnowledgebaseLeaseLost:
            logger.info(
                "knowledgebase indexing ownership lost",
                extra={"extra_data": {"job_id": job.job_id}},
            )
        except _KnowledgebaseGenerationStale:
            await self._cancel_stale_job(job.job_id, lease, lost)
        except Exception as exc:
            logger.warning(
                "knowledgebase indexing job failed",
                extra={"extra_data": {"job_id": job.job_id, "error": str(exc)}},
            )
            try:
                await self._finish_job(
                    job.job_id,
                    lease,
                    lost,
                    status="failed",
                    error=str(exc),
                )
            except _KnowledgebaseLeaseLost:
                logger.info(
                    "knowledgebase failure settlement skipped after ownership loss",
                    extra={"extra_data": {"job_id": job.job_id}},
                )
        finally:
            lost.set()
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await renewal
            try:
                await self._lease_store.release(lease)
            except Exception:
                logger.warning(
                    "knowledgebase lease release failed",
                    extra={"extra_data": {"resource_key": lease.resource_key}},
                    exc_info=True,
                )
        return True

    async def _renew_lease(self, lease: Lease, lost: asyncio.Event) -> None:
        try:
            current = lease
            while not lost.is_set():
                await asyncio.sleep(_JOB_LEASE_SECONDS / 3)
                renewed = await self._lease_store.renew(current, ttl_seconds=_JOB_LEASE_SECONDS)
                if renewed is None:
                    lost.set()
                    return
                current = renewed
        except asyncio.CancelledError:
            raise
        except Exception:
            lost.set()
            logger.warning(
                "knowledgebase lease renewal failed; job settlement is fenced",
                extra={"extra_data": {"resource_key": lease.resource_key}},
                exc_info=True,
            )

    async def _require_current(
        self,
        lease: Lease,
        session: Any | None = None,
        lost: asyncio.Event | None = None,
    ) -> None:
        if lost is not None and lost.is_set():
            raise _KnowledgebaseLeaseLost("knowledgebase index job lease renewal failed")
        current = (
            await self._lease_store.is_current_in_session(session, lease)
            if session is not None
            else await self._lease_store.is_current(lease)
        )
        if lost is not None and lost.is_set():
            raise _KnowledgebaseLeaseLost("knowledgebase index job lease renewal failed")
        if not current:
            raise _KnowledgebaseLeaseLost("knowledgebase index job lease lost")

    async def _run(self) -> None:
        while not self._stopped.is_set():
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self._poll_interval_seconds)

    async def _finish_job(
        self,
        job_id: str,
        lease: Lease,
        lost: asyncio.Event | None = None,
        *,
        status: str,
        error: str | None = None,
        chunks_indexed: int = 0,
        chunks_deleted: int = 0,
    ) -> None:
        async with self._session_factory() as session:
            await self._require_current(lease, session, lost)
            result = await session.execute(
                select(KnowledgebaseIndexJobRow).where(KnowledgebaseIndexJobRow.job_id == job_id)
            )
            job = result.scalar_one_or_none()
            if job is not None and job.status == "running":
                job.status = status
                job.error = error
                job.completed_at = datetime.now(UTC)
                job.updated_at = datetime.now(UTC)
                job.chunks_indexed = chunks_indexed
                job.chunks_deleted = chunks_deleted
                if (
                    status == "failed"
                    and job.kb_artifact_id is not None
                    and job.job_type not in _CLEANUP_JOB_TYPES
                ):
                    attachment = (
                        await session.execute(
                            select(KnowledgebaseArtifactRow).where(
                                KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id
                            )
                        )
                    ).scalar_one_or_none()
                    if (
                        attachment is not None
                        and attachment.desired_generation == job.generation
                        and attachment.status not in {"detached", "removed"}
                    ):
                        attachment.status = (
                            "stale" if attachment.active_generation > 0 else "failed"
                        )
                        attachment.last_error = error
                        attachment.last_job_id = job.job_id
                        attachment.updated_at = datetime.now(UTC)
            await session.commit()

    async def _cancel_stale_job(
        self,
        job_id: str,
        lease: Lease,
        lost: asyncio.Event | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await self._require_current(lease, session, lost)
            job = await session.get(KnowledgebaseIndexJobRow, job_id)
            if job is not None and job.status == "running":
                job.status = "cancelled"
                job.error = "superseded_generation"
                job.completed_at = datetime.now(UTC)
                job.updated_at = datetime.now(UTC)
            await session.commit()

    async def _require_job_generation(
        self,
        session: Any,
        job: KnowledgebaseIndexJobRow,
        lease: Lease,
        lost: asyncio.Event | None,
    ) -> tuple[KnowledgebaseArtifactRow, KnowledgebaseRow]:
        await self._require_current(lease, session, lost)
        current_job = await session.get(KnowledgebaseIndexJobRow, job.job_id)
        attachment = (
            await session.get(KnowledgebaseArtifactRow, job.kb_artifact_id)
            if job.kb_artifact_id is not None
            else None
        )
        knowledgebase = await session.get(KnowledgebaseRow, job.knowledgebase_id)
        if job.job_type == "delete_stale_vectors":
            if (
                current_job is None
                or current_job.status != "running"
                or attachment is None
                or knowledgebase is None
            ):
                raise _KnowledgebaseGenerationStale("stale vector cleanup was superseded")
            return attachment, knowledgebase
        if (
            current_job is None
            or current_job.status != "running"
            or attachment is None
            or knowledgebase is None
            or current_job.generation != attachment.desired_generation
        ):
            raise _KnowledgebaseGenerationStale("knowledgebase index generation was superseded")
        if job.job_type == "delete_artifact_index":
            if attachment.status not in {"detached", "removed"}:
                raise _KnowledgebaseGenerationStale("attachment is no longer pending deletion")
        elif (
            knowledgebase.status != "active"
            or attachment.status in {"detached", "removed"}
            or attachment.pending_artifact_id != current_job.artifact_id
        ):
            raise _KnowledgebaseGenerationStale("attachment is no longer pending this index")
        return attachment, knowledgebase

    async def _delete_stale_vectors(
        self,
        job: KnowledgebaseIndexJobRow,
        lease: Lease,
        lost: asyncio.Event | None = None,
    ) -> int:
        async with self._session_factory() as session:
            await self._require_job_generation(session, job, lease, lost)
        point_ids = (job.diagnostics or {}).get("point_ids")
        if not isinstance(point_ids, list) or not all(
            isinstance(point_id, str) and point_id for point_id in point_ids
        ):
            raise RuntimeError("stale vector cleanup job has invalid point_ids")
        await self._require_current(lease, lost=lost)
        await self._vector_backend.delete(point_ids=point_ids)
        return len(point_ids)

    async def _try_inline_stale_vector_cleanup(
        self, *, cleanup_job_id: str, point_ids: list[str]
    ) -> None:
        try:
            await self._vector_backend.delete(point_ids=point_ids)
        except Exception:
            logger.warning(
                "knowledgebase old generation vector cleanup deferred",
                extra={"extra_data": {"job_id": cleanup_job_id}},
                exc_info=True,
            )
            return
        async with self._session_factory() as session:
            cleanup_job = await session.get(KnowledgebaseIndexJobRow, cleanup_job_id)
            if cleanup_job is not None and cleanup_job.status == "queued":
                cleanup_job.status = "succeeded"
                cleanup_job.chunks_deleted = len(point_ids)
                cleanup_job.completed_at = datetime.now(UTC)
                cleanup_job.updated_at = datetime.now(UTC)
            await session.commit()

    async def _lock_generation_for_commit(
        self,
        session: Any,
        job: KnowledgebaseIndexJobRow,
        *,
        require_active_knowledgebase: bool,
    ) -> None:
        predicate = (KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id) & (
            KnowledgebaseArtifactRow.desired_generation == job.generation
        )
        if require_active_knowledgebase:
            predicate &= KnowledgebaseArtifactRow.pending_artifact_id == job.artifact_id
            predicate &= KnowledgebaseArtifactRow.status.not_in(["detached", "removed"])
            predicate &= (
                select(KnowledgebaseRow.knowledgebase_id)
                .where(
                    KnowledgebaseRow.knowledgebase_id == job.knowledgebase_id,
                    KnowledgebaseRow.status == "active",
                )
                .exists()
            )
        else:
            predicate &= KnowledgebaseArtifactRow.status.in_(["detached", "removed"])
        result = await session.execute(
            update(KnowledgebaseArtifactRow).where(predicate).values(updated_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            raise _KnowledgebaseGenerationStale("knowledgebase generation commit fence failed")

    async def _delete_index(
        self,
        job: KnowledgebaseIndexJobRow,
        lease: Lease,
        lost: asyncio.Event | None = None,
    ) -> int:
        if job.kb_artifact_id is None:
            return 0
        async with self._session_factory() as session:
            attachment, _ = await self._require_job_generation(session, job, lease, lost)
            await self._lock_generation_for_commit(session, job, require_active_knowledgebase=False)
            vector_ids = list(
                (
                    await session.execute(
                        select(KnowledgebaseChunkRow.vector_id).where(
                            KnowledgebaseChunkRow.kb_artifact_id == job.kb_artifact_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            await delete_knowledgebase_chunks(session, kb_artifact_id=job.kb_artifact_id)
            attachment.chunk_count = 0
            attachment.last_error = None
            await session.commit()
        if vector_ids:
            await self._vector_backend.delete(point_ids=vector_ids)
        await self._vector_backend.delete(
            filters={
                "knowledgebase_id": job.knowledgebase_id,
                "kb_artifact_id": job.kb_artifact_id,
            }
        )
        return len(vector_ids)

    async def _index_artifact(
        self,
        job: KnowledgebaseIndexJobRow,
        lease: Lease,
        lost: asyncio.Event | None = None,
    ) -> int:
        if job.kb_artifact_id is None or job.artifact_id is None:
            raise RuntimeError("Index job is missing artifact identifiers")
        async with self._session_factory() as session:
            attachment, knowledgebase = await self._require_job_generation(
                session, job, lease, lost
            )
            artifact = await get_artifact_record(session, job.artifact_id)
            if artifact is None:
                raise RuntimeError("Knowledgebase attachment or artifact not found")
            if artifact.status == "deleted":
                raise _KnowledgebaseGenerationStale("canonical artifact was deleted")
            attachment.status = "running"
            attachment.last_job_id = job.job_id
            attachment_metadata = dict(attachment.metadata_json or {})
            source_path = attachment.source_path
            kb_settings = dict(knowledgebase.settings or {})
            await session.commit()

        artifact = artifact  # keep pyright happy enough for runtime path
        if artifact.size_bytes > self._max_artifact_size_bytes:
            raise RuntimeError("artifact_too_large_for_indexing")
        content, _ = await self._artifact_store.async_load(
            artifact.namespace, artifact.object_id, artifact.filename
        )
        content_hash = hashlib.sha256(content).hexdigest()
        document = await extract_artifact_bytes_bounded(
            content, filename=artifact.filename, mime_type=artifact.mime_type
        )
        attachment_metadata = _merge_extracted_metadata(
            extracted=document.metadata,
            attached=attachment_metadata,
            metadata_schema=dict(knowledgebase.metadata_schema or {}),
        )
        max_chunks, target_tokens, overlap_tokens = _resolve_chunking_settings(
            kb_settings,
            default_max_chunks_per_artifact=self._max_chunks_per_artifact,
            default_target_tokens=self._chunk_target_tokens,
            default_overlap_tokens=self._chunk_overlap_tokens,
        )
        extracted_metadata = {
            key: value
            for key, value in document.metadata.items()
            if key not in _RESERVED_CHUNK_METADATA
        }
        user_metadata = {
            key: value
            for key, value in attachment_metadata.items()
            if key not in _RESERVED_CHUNK_METADATA
        }
        chunk_metadata = {
            **extracted_metadata,
            **user_metadata,
            "filename": artifact.filename,
            "mime_type": artifact.mime_type,
            "kind": artifact.kind,
            "purpose": artifact.purpose,
        }
        chunk_metadata.pop("source_path", None)
        if source_path is not None:
            chunk_metadata["source_path"] = source_path
        chunks = chunk_document(
            document,
            artifact_id=artifact.artifact_id,
            artifact_hash=content_hash,
            chunk_id_prefix=f"{job.kb_artifact_id}_g{job.generation}",
            metadata=chunk_metadata,
            max_chunks=max_chunks,
            target_tokens=target_tokens,
            overlap_tokens=overlap_tokens,
        )
        if not chunks:
            raise RuntimeError("no_extractable_text")
        vectors: list[list[float]] = []
        for index in range(0, len(chunks), self._embedding_batch_size):
            batch = chunks[index : index + self._embedding_batch_size]
            vectors.extend(
                await self._llm.embed([chunk.text for chunk in batch], task_type="embedding")
            )
        vector_size = len(vectors[0])
        vector_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"cognis-kb:{chunk.locator['chunk_id']}"))
            for chunk in chunks
        ]
        points = [
            VectorPoint(
                point_id=vector_id,
                vector=vector,
                sparse_vector=sparse_vector_from_text(chunk.text),
                payload={
                    **chunk.metadata,
                    "owner_email": getattr(artifact, "owner_email", None),
                    "knowledgebase_id": job.knowledgebase_id,
                    "kb_artifact_id": job.kb_artifact_id,
                    "generation": job.generation,
                    "artifact_id": artifact.artifact_id,
                    "artifact_hash": content_hash,
                    "chunk_id": chunk.locator["chunk_id"],
                },
            )
            for chunk, vector, vector_id in zip(chunks, vectors, vector_ids, strict=True)
        ]
        activated = False
        try:
            cleanup_job_id: str | None = None
            async with self._session_factory() as session:
                await self._require_job_generation(session, job, lease, lost)
            await self._vector_backend.upsert(points, vector_size=vector_size)
            async with self._session_factory() as session:
                attachment, _ = await self._require_job_generation(session, job, lease, lost)
                await self._lock_generation_for_commit(
                    session, job, require_active_knowledgebase=True
                )
                old_vector_ids = await delete_knowledgebase_chunks(
                    session, kb_artifact_id=job.kb_artifact_id
                )
                rows = [
                    KnowledgebaseChunkRow(
                        chunk_id=chunk.locator["chunk_id"],
                        knowledgebase_id=job.knowledgebase_id,
                        kb_artifact_id=job.kb_artifact_id,
                        artifact_id=artifact.artifact_id,
                        artifact_hash=content_hash,
                        generation=job.generation,
                        chunk_index=chunk.chunk_index,
                        text=chunk.text,
                        text_hash=chunk.text_hash,
                        token_count=chunk.token_count,
                        locator=chunk.locator,
                        metadata_json=chunk.metadata,
                        vector_id=vector_id,
                    )
                    for chunk, vector_id in zip(chunks, vector_ids, strict=True)
                ]
                await insert_knowledgebase_chunks(session, rows=rows)
                attachment.status = "indexed"
                attachment.artifact_id = artifact.artifact_id
                attachment.pending_artifact_id = None
                attachment.pending_source_hash = None
                attachment.active_generation = job.generation
                attachment.source_hash = content_hash
                attachment.source_size_bytes = artifact.size_bytes
                attachment.source_mime_type = artifact.mime_type
                attachment.source_filename = artifact.filename
                attachment.chunk_count = len(rows)
                attachment.vector_dimension = vector_size
                attachment.last_error = None
                attachment.active_metadata_json = attachment_metadata
                attachment.last_diagnostics = document.diagnostics
                attachment.indexed_at = datetime.now(UTC)
                attachment.stale_at = None
                art = await get_artifact_record(session, artifact.artifact_id)
                if art is not None:
                    art.content_hash = content_hash
                if old_vector_ids:
                    cleanup_job = await enqueue_knowledgebase_job(
                        session,
                        knowledgebase_id=job.knowledgebase_id,
                        kb_artifact_id=job.kb_artifact_id,
                        artifact_id=artifact.artifact_id,
                        job_type="delete_stale_vectors",
                        generation=job.generation,
                        priority=20,
                        diagnostics={"point_ids": old_vector_ids},
                    )
                    cleanup_job_id = cleanup_job.job_id
                await session.commit()
                activated = True
            if old_vector_ids and cleanup_job_id is not None:
                await self._try_inline_stale_vector_cleanup(
                    cleanup_job_id=cleanup_job_id,
                    point_ids=old_vector_ids,
                )
        finally:
            if not activated:
                with contextlib.suppress(Exception):
                    await self._vector_backend.delete(point_ids=vector_ids)
        return len(chunks)
