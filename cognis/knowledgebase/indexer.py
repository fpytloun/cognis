"""Background knowledgebase indexing worker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.knowledgebase.chunking import chunk_document
from cognis.knowledgebase.extraction import extract_artifact_bytes
from cognis.knowledgebase.vector import VectorPoint, sparse_vector_from_text
from cognis.logging import get_logger
from cognis.store.models import (
    KnowledgebaseArtifactRow,
    KnowledgebaseChunkRow,
    KnowledgebaseIndexJobRow,
    KnowledgebaseRow,
)
from cognis.store.queries import (
    claim_next_knowledgebase_job,
    delete_knowledgebase_chunks,
    get_artifact_record,
    insert_knowledgebase_chunks,
)

logger = get_logger(__name__)


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
            job = await claim_next_knowledgebase_job(session)
            await session.commit()
        if job is None:
            return False
        try:
            if job.job_type == "delete_artifact_index":
                chunks_deleted = await self._delete_index(job)
                await self._finish_job(
                    job.job_id, status="succeeded", chunks_deleted=chunks_deleted
                )
            else:
                chunks_indexed = await self._index_artifact(job)
                await self._finish_job(
                    job.job_id, status="succeeded", chunks_indexed=chunks_indexed
                )
        except Exception as exc:
            logger.warning(
                "knowledgebase indexing job failed",
                extra={"extra_data": {"job_id": job.job_id, "error": str(exc)}},
            )
            await self._finish_job(job.job_id, status="failed", error=str(exc))
        return True

    async def _run(self) -> None:
        while not self._stopped.is_set():
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self._poll_interval_seconds)

    async def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None = None,
        chunks_indexed: int = 0,
        chunks_deleted: int = 0,
    ) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgebaseIndexJobRow).where(KnowledgebaseIndexJobRow.job_id == job_id)
            )
            job = result.scalar_one_or_none()
            if job is not None:
                job.status = status
                job.error = error
                job.completed_at = datetime.now(UTC)
                job.updated_at = datetime.now(UTC)
                job.chunks_indexed = chunks_indexed
                job.chunks_deleted = chunks_deleted
                if status == "failed" and job.kb_artifact_id is not None:
                    attachment = (
                        await session.execute(
                            select(KnowledgebaseArtifactRow).where(
                                KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id
                            )
                        )
                    ).scalar_one_or_none()
                    if attachment is not None and attachment.status == "running":
                        attachment.status = "failed"
                        attachment.last_error = error
                        attachment.last_job_id = job.job_id
                        attachment.updated_at = datetime.now(UTC)
            await session.commit()

    async def _delete_index(self, job: KnowledgebaseIndexJobRow) -> int:
        if job.kb_artifact_id is None:
            return 0
        async with self._session_factory() as session:
            vector_ids = await delete_knowledgebase_chunks(
                session, kb_artifact_id=job.kb_artifact_id
            )
            result = await session.execute(
                select(KnowledgebaseArtifactRow).where(
                    KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id
                )
            )
            attachment = result.scalar_one_or_none()
            if attachment is not None:
                attachment.chunk_count = 0
                attachment.last_error = None
            await session.commit()
        if vector_ids:
            await self._vector_backend.delete(point_ids=vector_ids)
        return len(vector_ids)

    async def _index_artifact(self, job: KnowledgebaseIndexJobRow) -> int:
        if job.kb_artifact_id is None or job.artifact_id is None:
            raise RuntimeError("Index job is missing artifact identifiers")
        async with self._session_factory() as session:
            attachment = (
                await session.execute(
                    select(KnowledgebaseArtifactRow).where(
                        KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id
                    )
                )
            ).scalar_one_or_none()
            knowledgebase = (
                await session.execute(
                    select(KnowledgebaseRow).where(
                        KnowledgebaseRow.knowledgebase_id == job.knowledgebase_id
                    )
                )
            ).scalar_one_or_none()
            artifact = await get_artifact_record(session, job.artifact_id)
            if attachment is None or artifact is None or knowledgebase is None:
                raise RuntimeError("Knowledgebase attachment or artifact not found")
            if attachment.status in {"detached", "removed"}:
                return 0
            attachment.status = "running"
            attachment.last_job_id = job.job_id
            attachment_metadata = dict(attachment.metadata_json or {})
            kb_settings = dict(knowledgebase.settings or {})
            await session.commit()

        artifact = artifact  # keep pyright happy enough for runtime path
        if artifact.size_bytes > self._max_artifact_size_bytes:
            raise RuntimeError("artifact_too_large_for_indexing")
        content, _ = await self._artifact_store.async_load(
            artifact.namespace, artifact.object_id, artifact.filename
        )
        content_hash = hashlib.sha256(content).hexdigest()
        document = extract_artifact_bytes(
            content, filename=artifact.filename, mime_type=artifact.mime_type
        )
        max_chunks, target_tokens, overlap_tokens = _resolve_chunking_settings(
            kb_settings,
            default_max_chunks_per_artifact=self._max_chunks_per_artifact,
            default_target_tokens=self._chunk_target_tokens,
            default_overlap_tokens=self._chunk_overlap_tokens,
        )
        chunks = chunk_document(
            document,
            artifact_id=artifact.artifact_id,
            artifact_hash=content_hash,
            chunk_id_prefix=job.kb_artifact_id,
            metadata={
                "filename": artifact.filename,
                "mime_type": artifact.mime_type,
                "kind": artifact.kind,
                "purpose": artifact.purpose,
                **attachment_metadata,
            },
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
        async with self._session_factory() as session:
            old_vector_ids = await delete_knowledgebase_chunks(
                session, kb_artifact_id=job.kb_artifact_id
            )
            await session.commit()
        if old_vector_ids:
            await self._vector_backend.delete(point_ids=old_vector_ids)
        points = [
            VectorPoint(
                point_id=vector_id,
                vector=vector,
                sparse_vector=sparse_vector_from_text(chunk.text),
                payload={
                    "owner_email": getattr(artifact, "owner_email", None),
                    "knowledgebase_id": job.knowledgebase_id,
                    "kb_artifact_id": job.kb_artifact_id,
                    "artifact_id": artifact.artifact_id,
                    "artifact_hash": content_hash,
                    "chunk_id": chunk.locator["chunk_id"],
                    **chunk.metadata,
                },
            )
            for chunk, vector, vector_id in zip(chunks, vectors, vector_ids, strict=True)
        ]
        await self._vector_backend.upsert(points, vector_size=vector_size)
        async with self._session_factory() as session:
            rows = [
                KnowledgebaseChunkRow(
                    chunk_id=chunk.locator["chunk_id"],
                    knowledgebase_id=job.knowledgebase_id,
                    kb_artifact_id=job.kb_artifact_id,
                    artifact_id=artifact.artifact_id,
                    artifact_hash=content_hash,
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
            result = await session.execute(
                select(KnowledgebaseArtifactRow).where(
                    KnowledgebaseArtifactRow.kb_artifact_id == job.kb_artifact_id
                )
            )
            attachment = result.scalar_one_or_none()
            if attachment is not None:
                attachment.status = "indexed"
                attachment.source_hash = content_hash
                attachment.source_size_bytes = artifact.size_bytes
                attachment.source_mime_type = artifact.mime_type
                attachment.source_filename = artifact.filename
                attachment.chunk_count = len(rows)
                attachment.vector_dimension = vector_size
                attachment.last_error = None
                attachment.last_diagnostics = document.diagnostics
                attachment.indexed_at = datetime.now(UTC)
            art = await get_artifact_record(session, artifact.artifact_id)
            if art is not None:
                art.content_hash = content_hash
            await session.commit()
        return len(chunks)
