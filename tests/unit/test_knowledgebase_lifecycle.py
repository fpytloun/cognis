from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.bootstrap import run_schema_bootstrap
from cognis.knowledgebase import indexer as indexer_module
from cognis.knowledgebase.indexer import KnowledgebaseIndexer
from cognis.knowledgebase.service import KnowledgebaseService
from cognis.knowledgebase.vector import VectorPoint, VectorSearchHit
from cognis.models.knowledgebase import (
    KnowledgebaseFacetRequest,
    KnowledgebaseFilter,
    KnowledgebaseSearchRequest,
)
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    Base,
    KnowledgebaseArtifactRow,
    KnowledgebaseChunkRow,
    KnowledgebaseIndexJobRow,
    KnowledgebaseRow,
    User,
)
from cognis.store.queries import (
    assign_knowledgebase_to_agent,
    attach_artifact_to_knowledgebase,
    delete_artifact_record,
    delete_knowledgebase,
    detach_knowledgebase_artifact,
    enqueue_knowledgebase_artifact_reindex,
    enqueue_knowledgebase_job,
    enqueue_retry_knowledgebase_job,
    list_knowledgebase_chunks,
    mark_artifact_deleted,
    update_knowledgebase,
    upsert_model_routing,
)


class _ArtifactStore:
    def __init__(self, content: dict[str, bytes]) -> None:
        self.content = content

    async def async_load(self, namespace: str, object_id: str, filename: str) -> tuple[bytes, str]:
        del namespace, filename
        return self.content[object_id], "text/plain"


class _EmbeddingProvider:
    def __init__(self, *, block: bool = False, error: Exception | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.error = error

    async def embed(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        del kwargs
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return [[float(index + 1), 0.5] for index, _text in enumerate(texts)]


class _VectorBackend:
    name = "test"

    def __init__(self, *, delete_failures: int = 0) -> None:
        self.points: dict[str, VectorPoint] = {}
        self.delete_failures = delete_failures

    async def upsert(self, points: list[VectorPoint], *, vector_size: int) -> None:
        assert vector_size == 2
        self.points.update({point.point_id: point for point in points})

    async def delete(
        self,
        *,
        point_ids: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None:
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError("vector deletion unavailable")
        if point_ids:
            for point_id in point_ids:
                self.points.pop(point_id, None)
        if filters:
            self.points = {
                point_id: point
                for point_id, point in self.points.items()
                if not all(point.payload.get(key) == value for key, value in filters.items())
            }

    async def health(self) -> dict[str, Any]:
        return {"ok": True}

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        filters: dict[str, Any] | None = None,
        sparse_vector: Any | None = None,
    ) -> list[VectorSearchHit]:
        del vector, sparse_vector
        matches = []
        for point in self.points.values():
            if filters and not all(
                point.payload.get(key) in value
                if isinstance(value, list)
                else point.payload.get(key) == value
                for key, value in filters.items()
            ):
                continue
            matches.append(
                VectorSearchHit(point_id=point.point_id, score=1.0, payload=point.payload)
            )
        return matches[:limit]


async def _seed_replacement(
    tmp_path: Path,
) -> tuple[Any, async_sessionmaker[Any], _VectorBackend]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    vector = _VectorBackend()
    vector.points["old-vector"] = VectorPoint(
        point_id="old-vector",
        vector=[1.0, 0.5],
        payload={
            "owner_email": "owner@example.com",
            "knowledgebase_id": "kb-1",
            "kb_artifact_id": "kba-1",
            "generation": 1,
            "chunk_id": "kba-1_g1_000000",
            "source_path": "docs/guide.txt",
        },
    )
    async with factory() as session:
        session.add(User(email="owner@example.com", name="Owner", role="user"))
        session.add(
            KnowledgebaseRow(
                knowledgebase_id="kb-1",
                owner_email="owner@example.com",
                name="Knowledge",
            )
        )
        session.add_all(
            [
                ArtifactRecordRow(
                    artifact_id="artifact-old",
                    namespace="owner",
                    object_id="old",
                    filename="guide.txt",
                    owner_email="owner@example.com",
                    mime_type="text/plain",
                    size_bytes=11,
                    status="attached",
                ),
                ArtifactRecordRow(
                    artifact_id="artifact-new",
                    namespace="owner",
                    object_id="new",
                    filename="guide.txt",
                    owner_email="owner@example.com",
                    mime_type="text/plain",
                    size_bytes=15,
                    status="temporary",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                ),
            ]
        )
        session.add(
            KnowledgebaseArtifactRow(
                kb_artifact_id="kba-1",
                knowledgebase_id="kb-1",
                source_path="docs/guide.txt",
                artifact_id="artifact-old",
                active_generation=1,
                desired_generation=1,
                status="indexed",
                source_hash="old-hash",
                source_filename="guide.txt",
                source_mime_type="text/plain",
                chunk_count=1,
            )
        )
        session.add(
            KnowledgebaseChunkRow(
                chunk_id="kba-1_g1_000000",
                knowledgebase_id="kb-1",
                kb_artifact_id="kba-1",
                artifact_id="artifact-old",
                artifact_hash="old-hash",
                generation=1,
                chunk_index=0,
                text="old readable content",
                text_hash="old-text-hash",
                locator={
                    "chunk_id": "kba-1_g1_000000",
                    "chunk_index": 0,
                    "artifact_id": "artifact-old",
                    "extraction_method": "text",
                },
                vector_id="old-vector",
            )
        )
        await upsert_model_routing(session, task_type="embedding", provider_id=None, model="embed")
        await upsert_model_routing(session, task_type="default", provider_id=None, model="answer")
        await session.commit()
        replacement = await attach_artifact_to_knowledgebase(
            session,
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            artifact_id="artifact-new",
            source_path="docs/guide.txt",
            metadata={"category": "manual", "source_path": "spoofed/path.txt"},
        )
        assert replacement is not None
        await session.commit()
    return engine, factory, vector


def _indexer(
    factory: async_sessionmaker[Any],
    vector: _VectorBackend,
    llm: _EmbeddingProvider,
) -> KnowledgebaseIndexer:
    return KnowledgebaseIndexer(
        session_factory=factory,
        artifact_store=_ArtifactStore({"old": b"old content", "new": b"new replacement"}),
        llm=llm,
        vector_backend=vector,
        enabled=True,
        poll_interval_seconds=0.01,
        max_artifact_size_bytes=1024,
        max_chunks_per_artifact=10,
        chunk_target_tokens=100,
        chunk_overlap_tokens=0,
        embedding_batch_size=2,
        controller_owner_id="controller-test",
    )


def _service(
    factory: async_sessionmaker[Any],
    vector: _VectorBackend,
    llm: _EmbeddingProvider | None = None,
) -> KnowledgebaseService:
    return KnowledgebaseService(
        session_factory=factory,
        artifact_store=None,
        llm=llm or _EmbeddingProvider(),
        vector_backend=vector,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_duplicate_generation_job_is_deduplicated(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            first = await enqueue_knowledgebase_job(
                session,
                knowledgebase_id="kb-1",
                kb_artifact_id="kba-1",
                artifact_id="artifact-1",
                generation=4,
                job_type="reindex_artifact",
            )
            second = await enqueue_knowledgebase_job(
                session,
                knowledgebase_id="kb-1",
                kb_artifact_id="kba-1",
                artifact_id="artifact-1",
                generation=4,
                job_type="reindex_artifact",
            )
            assert second.job_id == first.job_id
            assert (
                await session.scalar(sa.select(sa.func.count(KnowledgebaseIndexJobRow.job_id))) == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replacement_activates_new_generation_then_cleans_old_vectors(
    tmp_path: Path,
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    try:
        assert await _indexer(factory, vector, _EmbeddingProvider()).run_once()
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            artifact = await session.get(ArtifactRecordRow, "artifact-new")
            chunks = await list_knowledgebase_chunks(session, knowledgebase_id="kb-1")
            assert attachment is not None
            assert artifact is not None
            assert artifact.status == "attached"
            assert artifact.expires_at is None
            assert attachment.artifact_id == "artifact-new"
            assert attachment.active_generation == attachment.desired_generation == 2
            assert attachment.pending_artifact_id is None
            assert [chunk.generation for chunk in chunks] == [2]
            assert chunks[0].chunk_id.startswith("kba-1_g2_")
            assert chunks[0].metadata_json["source_path"] == "docs/guide.txt"
        assert "old-vector" not in vector.points
        assert len(vector.points) == 1
        point = next(iter(vector.points.values()))
        assert point.payload["source_path"] == "docs/guide.txt"
        result = await _service(factory, vector).search(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            payload=KnowledgebaseSearchRequest(
                query="replacement",
                filters=[
                    KnowledgebaseFilter(
                        field="source_path",
                        op="eq",
                        value="docs/guide.txt",
                    )
                ],
            ),
        )
        assert result is not None
        assert [match.artifact_id for match in result.matches] == ["artifact-new"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_residual_filters_fill_limit_from_overfetched_hits(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'residual-filter.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    vector = _VectorBackend()
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            session.add(
                KnowledgebaseRow(
                    knowledgebase_id="kb-1",
                    owner_email="owner@example.com",
                    name="Knowledge",
                )
            )
            for index, filename in enumerate(
                ["drop-a.txt", "drop-b.txt", "keep-a.txt", "keep-b.txt"]
            ):
                artifact_id = f"artifact-{index}"
                kb_artifact_id = f"kba-{index}"
                chunk_id = f"{kb_artifact_id}_g1_000000"
                session.add(
                    ArtifactRecordRow(
                        artifact_id=artifact_id,
                        namespace="owner",
                        object_id=f"object-{index}",
                        filename=filename,
                        owner_email="owner@example.com",
                        mime_type="text/plain",
                        size_bytes=10,
                        status="attached",
                    )
                )
                session.add(
                    KnowledgebaseArtifactRow(
                        kb_artifact_id=kb_artifact_id,
                        knowledgebase_id="kb-1",
                        source_path=filename,
                        artifact_id=artifact_id,
                        active_generation=1,
                        desired_generation=1,
                        status="indexed",
                        source_hash=f"hash-{index}",
                        source_filename=filename,
                        source_mime_type="text/plain",
                        chunk_count=1,
                    )
                )
                session.add(
                    KnowledgebaseChunkRow(
                        chunk_id=chunk_id,
                        knowledgebase_id="kb-1",
                        kb_artifact_id=kb_artifact_id,
                        artifact_id=artifact_id,
                        artifact_hash=f"hash-{index}",
                        generation=1,
                        chunk_index=0,
                        text=f"content {filename}",
                        text_hash=f"text-hash-{index}",
                        metadata_json={"filename": filename, "mime_type": "text/plain"},
                        locator={
                            "chunk_id": chunk_id,
                            "chunk_index": 0,
                            "artifact_id": artifact_id,
                            "extraction_method": "text",
                        },
                        vector_id=f"vector-{index}",
                    )
                )
                vector.points[f"vector-{index}"] = VectorPoint(
                    point_id=f"vector-{index}",
                    vector=[1.0, 0.5],
                    payload={
                        "owner_email": "owner@example.com",
                        "knowledgebase_id": "kb-1",
                        "kb_artifact_id": kb_artifact_id,
                        "generation": 1,
                        "chunk_id": chunk_id,
                    },
                )
            await upsert_model_routing(
                session, task_type="embedding", provider_id=None, model="embed"
            )
            await session.commit()

        result = await _service(factory, vector).search(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            payload=KnowledgebaseSearchRequest(
                query="content",
                filters=[KnowledgebaseFilter(field="filename", op="contains", value="keep")],
                limit=2,
            ),
        )

        assert result is not None
        assert [match.citation.filename for match in result.matches] == [
            "keep-a.txt",
            "keep-b.txt",
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_indexer_uses_bounded_extraction_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    original = indexer_module.extract_artifact_bytes_bounded
    calls: list[str] = []

    async def tracked(
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        timeout_seconds: int = 30,
    ) -> Any:
        calls.append(filename)
        return await original(
            content,
            filename=filename,
            mime_type=mime_type,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(indexer_module, "extract_artifact_bytes_bounded", tracked)
    try:
        assert await _indexer(factory, vector, _EmbeddingProvider()).run_once()
        assert calls == ["guide.txt"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replacement_failure_preserves_last_good_generation(tmp_path: Path) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    try:
        assert await _indexer(
            factory, vector, _EmbeddingProvider(error=RuntimeError("embedding failed"))
        ).run_once()
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            chunks = await list_knowledgebase_chunks(session, knowledgebase_id="kb-1")
            assert attachment is not None
            assert attachment.status == "stale"
            assert attachment.artifact_id == "artifact-old"
            assert attachment.pending_artifact_id == "artifact-new"
            assert attachment.active_generation == 1
            assert attachment.desired_generation == 2
            assert [chunk.text for chunk in chunks] == ["old readable content"]
        assert set(vector.points) == {"old-vector"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_frontmatter_metadata_and_facets_switch_only_on_successful_generation(
    tmp_path: Path,
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    try:
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            artifact = await session.get(ArtifactRecordRow, "artifact-new")
            kb = await session.get(KnowledgebaseRow, "kb-1")
            assert attachment is not None and artifact is not None and kb is not None
            attachment.active_metadata_json = {
                "category": "manual",
                "title": "Old",
                "legacy": "remove-me",
            }
            artifact.filename = "guide.md"
            artifact.mime_type = "text/markdown"
            kb.metadata_schema = {
                "fields": {
                    "category": {
                        "type": "keyword",
                        "filterable": True,
                        "facetable": True,
                    },
                    "title": {
                        "type": "string",
                        "filterable": True,
                        "facetable": True,
                    },
                    "legacy": {
                        "type": "string",
                        "filterable": True,
                        "facetable": True,
                    },
                }
            }
            await session.commit()

        content = b"---\ntitle: New\n---\nnew replacement"
        failed_indexer = _indexer(
            factory, vector, _EmbeddingProvider(error=RuntimeError("embedding failed"))
        )
        failed_indexer._artifact_store = _ArtifactStore({"old": b"old content", "new": content})
        assert await failed_indexer.run_once()
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            failed = await session.get(KnowledgebaseIndexJobRow, attachment.last_job_id)
            assert attachment is not None and failed is not None
            assert attachment.active_generation == 1
            assert attachment.active_metadata_json == {
                "category": "manual",
                "title": "Old",
                "legacy": "remove-me",
            }
            retry = await enqueue_retry_knowledgebase_job(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                job_id=failed.job_id,
            )
            assert retry is not None
            await session.commit()
        stale_facets = await _service(factory, vector).facets(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            payload=KnowledgebaseFacetRequest(fields=["title", "legacy"]),
        )
        assert stale_facets is not None
        stale_fields = {field.field: field for field in stale_facets.fields}
        assert [(value.value, value.count) for value in stale_fields["title"].values] == [
            ("Old", 1)
        ]
        assert [(value.value, value.count) for value in stale_fields["legacy"].values] == [
            ("remove-me", 1)
        ]

        successful = _indexer(factory, vector, _EmbeddingProvider())
        successful._artifact_store = _ArtifactStore({"old": b"old content", "new": content})
        assert await successful.run_once()
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            assert attachment is not None
            assert attachment.active_generation == 3
            assert attachment.metadata_json == {
                "category": "manual",
                "source_path": "spoofed/path.txt",
            }
            assert attachment.active_metadata_json == {
                "category": "manual",
                "source_path": "spoofed/path.txt",
                "title": "New",
            }
        facets = await _service(factory, vector).facets(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            payload=KnowledgebaseFacetRequest(fields=["title", "legacy"]),
        )
        assert facets is not None
        fields = {field.field: field for field in facets.fields}
        assert [(value.value, value.count) for value in fields["title"].values] == [("New", 1)]
        assert fields["legacy"].values == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_old_generation_vector_cleanup_is_durable_and_eventually_retried(
    tmp_path: Path,
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    vector.delete_failures = 1
    indexer = _indexer(factory, vector, _EmbeddingProvider())
    try:
        assert await indexer.run_once()
        assert "old-vector" in vector.points
        async with factory() as session:
            cleanup = await session.scalar(
                sa.select(KnowledgebaseIndexJobRow).where(
                    KnowledgebaseIndexJobRow.job_type == "delete_stale_vectors"
                )
            )
            assert cleanup is not None
            assert cleanup.status == "queued"
            assert cleanup.diagnostics == {"point_ids": ["old-vector"]}
        assert await indexer.run_once()
        assert "old-vector" not in vector.points
        async with factory() as session:
            cleanup = await session.get(KnowledgebaseIndexJobRow, cleanup.job_id)
            assert cleanup is not None
            assert cleanup.status == "succeeded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_vector_cleanup_survives_newer_reindex_generation(tmp_path: Path) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    vector.delete_failures = 1
    indexer = _indexer(factory, vector, _EmbeddingProvider())
    try:
        assert await indexer.run_once()
        async with factory() as session:
            cleanup = await session.scalar(
                sa.select(KnowledgebaseIndexJobRow).where(
                    KnowledgebaseIndexJobRow.job_type == "delete_stale_vectors"
                )
            )
            assert cleanup is not None and cleanup.status == "queued"
            reindex = await enqueue_knowledgebase_artifact_reindex(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                artifact_id="artifact-new",
            )
            assert reindex is not None
            await session.commit()
            await session.refresh(cleanup)
            assert cleanup.status == "queued"
        assert await indexer.run_once()
        assert "old-vector" not in vector.points
    finally:
        await engine.dispose()


@pytest.mark.parametrize("inflight", [False, True])
@pytest.mark.asyncio
async def test_pending_canonical_deletion_cancels_replacement_without_removing_active(
    tmp_path: Path, inflight: bool
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    llm = _EmbeddingProvider(block=inflight)
    indexer = _indexer(factory, vector, llm)
    worker: asyncio.Task[bool] | None = None
    if inflight:
        worker = asyncio.create_task(indexer.run_once())
        await llm.entered.wait()
    async with factory() as session:
        assert await mark_artifact_deleted(session, "artifact-new")
        await session.commit()
    if worker is not None:
        llm.release.set()
        assert await worker
    async with factory() as session:
        attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
        chunks = await list_knowledgebase_chunks(session, knowledgebase_id="kb-1")
        assert attachment is not None
        assert attachment.status == "indexed"
        assert attachment.artifact_id == "artifact-old"
        assert attachment.pending_artifact_id is None
        assert attachment.active_generation == 1
        assert [chunk.text for chunk in chunks] == ["old readable content"]
    assert set(vector.points) == {"old-vector"}
    await engine.dispose()


@pytest.mark.parametrize(
    ("delete_operation", "delete_first"),
    [
        (mark_artifact_deleted, False),
        (delete_artifact_record, False),
        (mark_artifact_deleted, True),
        (delete_artifact_record, True),
    ],
)
@pytest.mark.asyncio
async def test_canonical_delete_serializes_with_concurrent_replacement(
    tmp_path: Path,
    delete_operation: Any,
    delete_first: bool,
) -> None:
    engine, factory, _vector = await _seed_replacement(tmp_path)
    async with factory() as session:
        session.add(
            ArtifactRecordRow(
                artifact_id="artifact-race",
                namespace="owner",
                object_id="race",
                filename="guide.txt",
                owner_email="owner@example.com",
                mime_type="text/plain",
                size_bytes=4,
                status="temporary",
            )
        )
        await session.commit()

    async def replace() -> KnowledgebaseArtifactRow | None:
        async with factory() as session:
            result = await attach_artifact_to_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                artifact_id="artifact-race",
                source_path="docs/guide.txt",
            )
            await session.commit()
            return result

    async def remove() -> bool:
        async with factory() as session:
            result = await delete_operation(session, "artifact-race")
            await session.commit()
            return result

    try:
        if delete_first:
            async with factory() as delete_session:
                assert await delete_operation(delete_session, "artifact-race")
                replacement = asyncio.create_task(replace())
                await asyncio.sleep(0.05)
                assert not replacement.done()
                await delete_session.commit()
            assert await replacement is None
        else:
            async with factory() as replace_session:
                attached = await attach_artifact_to_knowledgebase(
                    replace_session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-race",
                    source_path="docs/guide.txt",
                )
                assert attached is not None
                deletion = asyncio.create_task(remove())
                await asyncio.sleep(0.05)
                assert not deletion.done()
                await replace_session.commit()
            assert await deletion

        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            artifact = await session.get(ArtifactRecordRow, "artifact-race")
            assert attachment is not None
            assert attachment.pending_artifact_id != "artifact-race"
            if delete_operation is delete_artifact_record:
                assert artifact is None
            else:
                assert artifact is not None
                assert artifact.status == "deleted"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_reindex_allocates_distinct_generations_and_one_live_job(
    tmp_path: Path,
) -> None:
    engine, factory, _vector = await _seed_replacement(tmp_path)

    async def enqueue() -> str:
        async with factory() as session:
            job = await enqueue_knowledgebase_artifact_reindex(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                artifact_id="artifact-old",
            )
            assert job is not None
            await session.commit()
            return job.job_id

    try:
        job_ids = await asyncio.gather(enqueue(), enqueue())
        assert len(set(job_ids)) == 2
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            live_jobs = list(
                (
                    await session.execute(
                        sa.select(KnowledgebaseIndexJobRow).where(
                            KnowledgebaseIndexJobRow.kb_artifact_id == "kba-1",
                            KnowledgebaseIndexJobRow.status.in_(["queued", "running"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert attachment is not None
            assert len(live_jobs) == 1
            assert live_jobs[0].generation == attachment.desired_generation
    finally:
        await engine.dispose()


@pytest.mark.parametrize("knowledgebase_status", ["archived", "deleted"])
@pytest.mark.asyncio
async def test_failed_terminal_vector_cleanup_remains_retryable_after_kb_deactivation(
    tmp_path: Path, knowledgebase_status: str
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    vector.delete_failures = 1
    indexer = _indexer(factory, vector, _EmbeddingProvider())
    try:
        async with factory() as session:
            detached = await detach_knowledgebase_artifact(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                artifact_id="artifact-old",
            )
            assert detached is not None
            await session.commit()
        assert await indexer.run_once()
        async with factory() as session:
            failed = await session.scalar(
                sa.select(KnowledgebaseIndexJobRow).where(
                    KnowledgebaseIndexJobRow.job_type == "delete_artifact_index",
                    KnowledgebaseIndexJobRow.status == "failed",
                )
            )
            assert failed is not None
            if knowledgebase_status == "archived":
                changed = await update_knowledgebase(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    updates={"status": "archived"},
                )
            else:
                changed = await delete_knowledgebase(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                )
            assert changed
            await session.commit()
        async with factory() as session:
            retry = await enqueue_retry_knowledgebase_job(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                job_id=failed.job_id,
            )
            assert retry is not None
            await session.commit()
        while vector.points:
            assert await indexer.run_once()
        assert vector.points == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_discovers_failed_cleanup_after_delete_then_retries(
    tmp_path: Path,
) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    vector.delete_failures = 1
    indexer = _indexer(factory, vector, _EmbeddingProvider())
    service = _service(factory, vector)
    try:
        async with factory() as session:
            assert await delete_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
            )
            await session.commit()

        assert await indexer.run_once()

        async with factory() as session:
            failed_job = await session.scalar(
                sa.select(KnowledgebaseIndexJobRow).where(
                    KnowledgebaseIndexJobRow.knowledgebase_id == "kb-1",
                    KnowledgebaseIndexJobRow.job_type == "delete_artifact_index",
                    KnowledgebaseIndexJobRow.status == "failed",
                )
            )
            assert failed_job is not None
            session.add_all(
                [
                    KnowledgebaseIndexJobRow(
                        job_id=f"newer-cleanup-{index}",
                        knowledgebase_id="kb-1",
                        kb_artifact_id="kba-1",
                        artifact_id="artifact-old",
                        generation=failed_job.generation,
                        job_type="delete_stale_vectors",
                        status="succeeded",
                        diagnostics={"point_ids": [f"already-removed-{index}"]},
                        queued_at=failed_job.queued_at + timedelta(seconds=index + 1),
                    )
                    for index in range(101)
                ]
            )
            await session.commit()

        jobs = await service.jobs(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
        )
        assert jobs is not None
        assert len(jobs) == 1
        failed = next(job for job in jobs if job.status == "failed")
        assert failed.job_id == failed_job.job_id

        retry = await service.retry_job(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            job_id=failed.job_id,
        )
        assert retry is not None
        while vector.points:
            assert await indexer.run_once()
        assert vector.points == {}
    finally:
        await engine.dispose()


@pytest.mark.parametrize("lifecycle", ["archive", "detach", "delete"])
@pytest.mark.asyncio
async def test_lifecycle_change_fences_inflight_replacement(tmp_path: Path, lifecycle: str) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    llm = _EmbeddingProvider(block=True)
    indexer = _indexer(factory, vector, llm)
    worker = asyncio.create_task(indexer.run_once())
    await llm.entered.wait()
    async with factory() as session:
        if lifecycle == "archive":
            assert await update_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                updates={"status": "archived"},
            )
        elif lifecycle == "detach":
            assert await detach_knowledgebase_artifact(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                artifact_id="artifact-old",
            )
        else:
            assert await delete_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
            )
        await session.commit()
    llm.release.set()
    assert await worker

    async with factory() as session:
        attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
        chunks = await list_knowledgebase_chunks(session, knowledgebase_id="kb-1")
        assert attachment is not None
        assert attachment.active_generation == 1
        if lifecycle == "archive":
            assert [chunk.text for chunk in chunks] == ["old readable content"]
        else:
            assert chunks == []
            assert await indexer.run_once()
            assert vector.points == {}
    if lifecycle == "archive":
        assert set(vector.points) == {"old-vector"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_archived_knowledgebase_rejects_mutations_except_reactivation(
    tmp_path: Path,
) -> None:
    engine, factory, _vector = await _seed_replacement(tmp_path)
    try:
        async with factory() as session:
            row = await session.get(KnowledgebaseRow, "kb-1")
            assert row is not None
            row.status = "archived"
            job = await session.scalar(sa.select(KnowledgebaseIndexJobRow))
            assert job is not None
            job.status = "failed"
            await session.commit()
        async with factory() as session:
            with pytest.raises(ValueError, match="only allows reactivation"):
                await update_knowledgebase(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    updates={"settings": {"chunking": {"target_tokens": 500}}},
                )
            assert (
                await attach_artifact_to_knowledgebase(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-new",
                )
                is None
            )
            assert (
                await enqueue_knowledgebase_artifact_reindex(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-old",
                )
                is None
            )
            assert (
                await detach_knowledgebase_artifact(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-old",
                )
                is None
            )
            assert (
                await enqueue_retry_knowledgebase_job(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    job_id=job.job_id,
                )
                is None
            )
            assert not await assign_knowledgebase_to_agent(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                agent_id="missing-agent",
            )
            reactivated = await update_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                updates={"status": "active"},
            )
            assert reactivated is not None
            assert reactivated.status == "active"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_archived_knowledgebase_remains_searchable(tmp_path: Path) -> None:
    engine, factory, vector = await _seed_replacement(tmp_path)
    try:
        async with factory() as session:
            archived = await update_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                updates={"status": "archived"},
            )
            assert archived is not None
            await session.commit()

        result = await _service(factory, vector).search(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            payload=KnowledgebaseSearchRequest(query="old content"),
        )
        assert result is not None
        assert [match.artifact_id for match in result.matches] == ["artifact-old"]

        async with factory() as session:
            assert (
                await enqueue_knowledgebase_artifact_reindex(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-old",
                )
                is None
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reactivation_requeues_initial_generation_cancelled_by_archive(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reactivate.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            session.add(
                KnowledgebaseRow(
                    knowledgebase_id="kb-1",
                    owner_email="owner@example.com",
                    name="Knowledge",
                )
            )
            session.add(
                ArtifactRecordRow(
                    artifact_id="artifact-1",
                    namespace="owner",
                    object_id="new",
                    filename="guide.txt",
                    owner_email="owner@example.com",
                    mime_type="text/plain",
                    size_bytes=15,
                    status="temporary",
                )
            )
            await session.commit()
            attachment = await attach_artifact_to_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                artifact_id="artifact-1",
                source_path="docs/guide.txt",
            )
            assert attachment is not None
            await session.commit()
            archived = await update_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                updates={"status": "archived"},
            )
            assert archived is not None
            await session.commit()
            assert attachment.pending_artifact_id == "artifact-1"
            assert attachment.status == "failed"
            reactivated = await update_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                updates={"status": "active"},
            )
            assert reactivated is not None
            await session.commit()
            await session.refresh(attachment)
            assert attachment.pending_artifact_id == "artifact-1"
            assert attachment.status == "queued"
            assert attachment.desired_generation == 3
            live_jobs = list(
                (
                    await session.execute(
                        sa.select(KnowledgebaseIndexJobRow).where(
                            KnowledgebaseIndexJobRow.kb_artifact_id == attachment.kb_artifact_id,
                            KnowledgebaseIndexJobRow.status == "queued",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(live_jobs) == 1
            assert live_jobs[0].generation == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_delete_cleans_assignments_and_preserves_canonical_artifacts(
    tmp_path: Path,
) -> None:
    engine, factory, _vector = await _seed_replacement(tmp_path)
    try:
        async with factory() as session:
            session.add(
                Agent(
                    agent_id="agent-1",
                    owner_email="owner@example.com",
                    name="Agent",
                    status="active",
                    permissions={"allowed_knowledgebases": ["kb-1", "kb-other"]},
                )
            )
            await session.commit()
            assert await delete_knowledgebase(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
            )
            await session.commit()
        async with factory() as session:
            agent = await session.get(Agent, "agent-1")
            knowledgebase = await session.get(KnowledgebaseRow, "kb-1")
            old_artifact = await session.get(ArtifactRecordRow, "artifact-old")
            new_artifact = await session.get(ArtifactRecordRow, "artifact-new")
            assert agent is not None
            assert agent.permissions["allowed_knowledgebases"] == ["kb-other"]
            assert knowledgebase is not None and knowledgebase.status == "deleted"
            assert old_artifact is not None
            assert new_artifact is not None
    finally:
        await engine.dispose()


def _migration_config(database_path: Path) -> Config:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    return config


def _default_is_zero(value: Any) -> bool:
    return str(value).strip("()'\" ") == "0"


def _default_is_current_timestamp(value: Any) -> bool:
    return str(value).strip("()'\" ").upper() in {"CURRENT_TIMESTAMP", "NOW"}


def test_generation_lifecycle_migration_backfills_and_has_expected_shape(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    config = _migration_config(database_path)
    command.upgrade(config, "105_managed_join_handoffs")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (email, name, password_hash, role, created_at, updated_at) "
                "VALUES ('owner@example.com', 'Owner', 'hash', 'user', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO knowledgebases "
                "(knowledgebase_id, owner_email, name, status, created_at, updated_at) "
                "VALUES ('kb-1', 'owner@example.com', 'KB', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO knowledgebase_artifacts "
                "(kb_artifact_id, knowledgebase_id, artifact_id, status, chunk_count, "
                "attached_at, updated_at) "
                "VALUES ('kba-1', 'kb-1', 'artifact-1', 'indexed', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO knowledgebase_chunks "
                "(chunk_id, knowledgebase_id, kb_artifact_id, artifact_id, chunk_index, "
                "text, text_hash, locator, created_at) "
                "VALUES ('chunk-1', 'kb-1', 'kba-1', 'artifact-1', 0, "
                "'text', 'hash', '{}', CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO knowledgebase_index_jobs "
                "(job_id, knowledgebase_id, kb_artifact_id, artifact_id, job_type, status, "
                "priority, attempts, chunks_indexed, chunks_deleted, queued_at, updated_at) "
                "VALUES ('job-1', 'kb-1', 'kba-1', 'artifact-1', 'reindex_artifact', "
                "'queued', 100, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            attachment_columns = {
                column["name"]: column
                for column in inspector.get_columns("knowledgebase_artifacts")
            }
            chunk_columns = {
                column["name"]: column for column in inspector.get_columns("knowledgebase_chunks")
            }
            job_columns = {
                column["name"]: column
                for column in inspector.get_columns("knowledgebase_index_jobs")
            }
            attachment_indexes = {
                index["name"] for index in inspector.get_indexes("knowledgebase_artifacts")
            }
            chunk_indexes = {
                index["name"] for index in inspector.get_indexes("knowledgebase_chunks")
            }
            job_indexes = {
                index["name"] for index in inspector.get_indexes("knowledgebase_index_jobs")
            }
            generations = connection.execute(
                sa.text(
                    "SELECT active_generation, desired_generation "
                    "FROM knowledgebase_artifacts WHERE kb_artifact_id = 'kba-1'"
                )
            ).one()
            chunk_generation = connection.scalar(
                sa.text("SELECT generation FROM knowledgebase_chunks WHERE chunk_id = 'chunk-1'")
            )
            job = connection.execute(
                sa.text(
                    "SELECT generation, status FROM knowledgebase_index_jobs WHERE job_id = 'job-1'"
                )
            ).one()
        assert {
            "source_path",
            "pending_artifact_id",
            "pending_source_hash",
            "active_generation",
            "desired_generation",
        } <= set(attachment_columns)
        assert _default_is_zero(attachment_columns["active_generation"]["default"])
        assert _default_is_zero(attachment_columns["desired_generation"]["default"])
        assert _default_is_zero(chunk_columns["generation"]["default"])
        assert _default_is_zero(job_columns["generation"]["default"])
        assert "uq_kb_artifacts_live_source_path" in attachment_indexes
        assert "ix_kb_chunks_attachment_generation_index" in chunk_indexes
        assert "uq_kb_jobs_live_attachment_generation_type" in job_indexes
        assert tuple(generations) == (1, 1)
        assert chunk_generation == 1
        assert tuple(job) == (0, "cancelled")
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_schema_bootstrap_upgrades_previous_kb_shape_twice(tmp_path: Path) -> None:
    database_path = tmp_path / "bootstrap.db"
    config = _migration_config(database_path)
    command.upgrade(config, "105_managed_join_handoffs")
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO users "
                    "(email, name, password_hash, role, created_at, updated_at) "
                    "VALUES ('owner@example.com', 'Owner', 'hash', 'user', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledgebases "
                    "(knowledgebase_id, owner_email, name, status, metadata_schema, "
                    "created_at, updated_at) "
                    "VALUES ('kb-1', 'owner@example.com', 'KB', 'active', :schema, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "schema": json.dumps(
                        {
                            "fields": {
                                "category": {
                                    "type": "keyword",
                                    "filterable": True,
                                    "facetable": True,
                                }
                            }
                        }
                    )
                },
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledgebase_artifacts "
                    "(kb_artifact_id, knowledgebase_id, artifact_id, status, chunk_count, "
                    "metadata, attached_at, updated_at) "
                    "VALUES ('kba-1', 'kb-1', 'artifact-1', 'indexed', 1, "
                    ":metadata, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"metadata": json.dumps({"category": "legacy"})},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledgebase_chunks "
                    "(chunk_id, knowledgebase_id, kb_artifact_id, artifact_id, chunk_index, "
                    "text, text_hash, locator, created_at) "
                    "VALUES ('chunk-1', 'kb-1', 'kba-1', 'artifact-1', 0, "
                    "'text', 'hash', '{}', CURRENT_TIMESTAMP)"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledgebase_index_jobs "
                    "(job_id, knowledgebase_id, kb_artifact_id, artifact_id, job_type, status, "
                    "priority, attempts, chunks_indexed, chunks_deleted, queued_at, updated_at) "
                    "VALUES ('job-1', 'kb-1', 'kba-1', 'artifact-1', 'reindex_artifact', "
                    "'queued', 100, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
        await run_schema_bootstrap(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.connect() as connection:
            active_metadata = await connection.scalar(
                sa.text(
                    "SELECT active_metadata FROM knowledgebase_artifacts "
                    "WHERE kb_artifact_id = 'kba-1'"
                )
            )
        assert json.loads(active_metadata) == {"category": "legacy"}
        facets = await _service(factory, _VectorBackend()).facets(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            payload=KnowledgebaseFacetRequest(fields=["category"]),
        )
        assert facets is not None
        assert [(value.value, value.count) for value in facets.fields[0].values] == [("legacy", 1)]
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            state = await connection.run_sync(
                lambda sync_connection: {
                    table: {
                        "columns": {
                            column["name"]: column
                            for column in inspect(sync_connection).get_columns(table)
                        },
                        "indexes": {
                            index["name"] for index in inspect(sync_connection).get_indexes(table)
                        },
                    }
                    for table in (
                        "knowledgebase_artifacts",
                        "knowledgebase_chunks",
                        "knowledgebase_index_jobs",
                    )
                }
            )
            attachment_generation = (
                await connection.execute(
                    sa.text(
                        "SELECT active_generation, desired_generation "
                        "FROM knowledgebase_artifacts WHERE kb_artifact_id = 'kba-1'"
                    )
                )
            ).one()
            chunk_generation = await connection.scalar(
                sa.text("SELECT generation FROM knowledgebase_chunks WHERE chunk_id = 'chunk-1'")
            )
            job_status = await connection.scalar(
                sa.text("SELECT status FROM knowledgebase_index_jobs WHERE job_id = 'job-1'")
            )
        assert {
            "source_path",
            "pending_artifact_id",
            "pending_source_hash",
            "active_generation",
            "desired_generation",
        } <= set(state["knowledgebase_artifacts"]["columns"])
        assert "generation" in state["knowledgebase_chunks"]["columns"]
        assert "generation" in state["knowledgebase_index_jobs"]["columns"]
        assert _default_is_zero(
            state["knowledgebase_artifacts"]["columns"]["active_generation"]["default"]
        )
        assert _default_is_zero(
            state["knowledgebase_artifacts"]["columns"]["desired_generation"]["default"]
        )
        assert _default_is_zero(state["knowledgebase_chunks"]["columns"]["generation"]["default"])
        assert _default_is_zero(
            state["knowledgebase_index_jobs"]["columns"]["generation"]["default"]
        )
        assert "uq_kb_artifacts_live_source_path" in state["knowledgebase_artifacts"]["indexes"]
        assert (
            "ix_kb_chunks_attachment_generation_index" in state["knowledgebase_chunks"]["indexes"]
        )
        assert (
            "uq_kb_jobs_live_attachment_generation_type"
            in state["knowledgebase_index_jobs"]["indexes"]
        )
        assert tuple(attachment_generation) == (1, 1)
        assert chunk_generation == 1
        assert job_status == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_upgrade_accepts_schema_already_updated_by_bootstrap(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bootstrap-then-migration.db"
    config = _migration_config(database_path)
    command.upgrade(config, "105_managed_join_handoffs")
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    await run_schema_bootstrap(engine)
    await engine.dispose()

    command.upgrade(config, "head")
    sync_engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with sync_engine.connect() as connection:
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("knowledgebase_artifacts")
            }
            assert {"active_generation", "desired_generation", "pending_artifact_id"} <= set(
                columns
            )
            assert _default_is_zero(columns["active_generation"]["default"])
            assert _default_is_zero(columns["desired_generation"]["default"])
    finally:
        sync_engine.dispose()


@pytest.mark.asyncio
async def test_knowledgebase_grant_bootstrap_is_idempotent(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/grants.db")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            state = await connection.run_sync(
                lambda sync: {
                    "columns": {
                        column["name"]: column
                        for column in inspect(sync).get_columns("knowledgebase_grants")
                    },
                    "indexes": {
                        index["name"]: index
                        for index in inspect(sync).get_indexes("knowledgebase_grants")
                    },
                    "foreign_keys": inspect(sync).get_foreign_keys("knowledgebase_grants"),
                }
            )
        assert {"grant_id", "knowledgebase_id", "grantee_user_email", "permission"} <= set(
            state["columns"]
        )
        assert str(state["columns"]["permission"]["default"]).strip("'\"") == "view"
        assert _default_is_current_timestamp(state["columns"]["granted_at"]["default"])
        assert "uq_knowledgebase_grants_active_user" in state["indexes"]
        active_index = state["indexes"]["uq_knowledgebase_grants_active_user"]
        assert bool(active_index["unique"]) is True
        assert active_index["column_names"] == ["knowledgebase_id", "grantee_user_email"]
        assert "revoked_at IS NULL" in str(active_index["dialect_options"]["sqlite_where"])
        foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key
            for foreign_key in state["foreign_keys"]
        }
        assert foreign_keys[("knowledgebase_id",)]["referred_table"] == "knowledgebases"
        assert foreign_keys[("knowledgebase_id",)]["options"]["ondelete"] == "CASCADE"
        assert foreign_keys[("grantee_user_email",)]["referred_table"] == "users"
        assert foreign_keys[("granted_by",)]["referred_table"] == "users"
    finally:
        await engine.dispose()


def test_knowledgebase_grant_migration_has_orm_parity(tmp_path: Path) -> None:
    database_path = tmp_path / "knowledgebase-grants-migration.db"
    command.upgrade(_migration_config(database_path), "head")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            columns = {
                column["name"]: column for column in inspector.get_columns("knowledgebase_grants")
            }
            indexes = {
                index["name"]: index for index in inspector.get_indexes("knowledgebase_grants")
            }
            foreign_keys = {
                tuple(foreign_key["constrained_columns"]): foreign_key
                for foreign_key in inspector.get_foreign_keys("knowledgebase_grants")
            }
        assert str(columns["permission"]["default"]).strip("'\"") == "view"
        assert _default_is_current_timestamp(columns["granted_at"]["default"])
        assert bool(indexes["uq_knowledgebase_grants_active_user"]["unique"]) is True
        assert indexes["uq_knowledgebase_grants_active_user"]["column_names"] == [
            "knowledgebase_id",
            "grantee_user_email",
        ]
        assert "revoked_at IS NULL" in str(
            indexes["uq_knowledgebase_grants_active_user"]["dialect_options"]["sqlite_where"]
        )
        assert {"ix_knowledgebase_grants_grantee", "ix_knowledgebase_grants_kb"} <= set(indexes)
        assert foreign_keys[("knowledgebase_id",)]["referred_table"] == "knowledgebases"
        assert foreign_keys[("knowledgebase_id",)]["options"]["ondelete"] == "CASCADE"
        assert foreign_keys[("grantee_user_email",)]["referred_table"] == "users"
        assert foreign_keys[("granted_by",)]["referred_table"] == "users"
    finally:
        engine.dispose()


def test_active_metadata_migration_backfills_last_good_generation(tmp_path: Path) -> None:
    database_path = tmp_path / "active-metadata.db"
    config = _migration_config(database_path)
    command.upgrade(config, "107_knowledgebase_grants")
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO users "
                    "(email, name, password_hash, role, created_at, updated_at) "
                    "VALUES ('owner@example.com', 'Owner', 'hash', 'user', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledgebases "
                    "(knowledgebase_id, owner_email, name, status, created_at, updated_at) "
                    "VALUES ('kb-1', 'owner@example.com', 'KB', 'active', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledgebase_artifacts "
                    "(kb_artifact_id, knowledgebase_id, active_generation, "
                    "desired_generation, status, metadata, chunk_count, attached_at, updated_at) "
                    "VALUES ('kba-1', 'kb-1', 1, 2, 'stale', "
                    '\'{"category":"old"}\', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)'
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("knowledgebase_artifacts")
            }
            value = connection.scalar(
                sa.text(
                    "SELECT active_metadata FROM knowledgebase_artifacts "
                    "WHERE kb_artifact_id = 'kba-1'"
                )
            )
        assert "active_metadata" in columns
        assert json.loads(value) == {"category": "old"}
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_active_metadata_bootstrap_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "active-metadata-bootstrap.db"
    config = _migration_config(database_path)
    command.upgrade(config, "107_knowledgebase_grants")
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"]
                    for column in inspect(sync).get_columns("knowledgebase_artifacts")
                }
            )
        assert "active_metadata" in columns
    finally:
        await engine.dispose()
