from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.core.agent_loop import AgentLoop
from cognis.core.agent_management import (
    AgentManagementDependencies,
    AgentManagementError,
    handle_agent_management_action,
)
from cognis.core.agent_management_catalog import assignable_knowledgebases
from cognis.knowledgebase.access import KnowledgebaseAccessContext
from cognis.knowledgebase.extraction import image_mime_type, supports_artifact_type
from cognis.knowledgebase.image_extraction import ImageExtractionError, extract_image
from cognis.knowledgebase.indexer import KnowledgebaseIndexer
from cognis.knowledgebase.service import KnowledgebaseRequestError, KnowledgebaseService
from cognis.models.agent import AgentPermissions
from cognis.models.knowledgebase import KnowledgebaseSearchRequest
from cognis.store.models import Agent, ArtifactRecordRow, Base, KnowledgebaseArtifactRow, User
from cognis.store.queries import (
    detach_knowledgebase_artifact,
    enqueue_knowledgebase_artifact_reindex,
    enqueue_retry_knowledgebase_job,
    list_knowledgebase_chunks,
    upsert_model_routing,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL
from cognis.tools.builtin.tool_search import CALL_TOOL_TOOL
from cognis.tools.introspection import describe_available_tool
from tests.unit.test_knowledgebase_lifecycle import (
    _ArtifactStore,
    _indexer,
    _seed_replacement,
    _VectorBackend,
)


def _vision(output: str | None = None) -> SimpleNamespace:
    output = (
        output
        if output is not None
        else json.dumps(
            {
                "transcription": "Egypt 2026. Hotel Dahab. Reservation 7340.",
                "description": "A printed reservation on a desk.",
                "uncertainties": "The arrival day is cropped.",
            }
        )
    )
    return SimpleNamespace(
        get_model_info=AsyncMock(return_value=SimpleNamespace(supports_vision=True)),
        generate=AsyncMock(return_value={"choices": [{"message": {"content": output}}]}),
        embed=AsyncMock(side_effect=lambda texts, **kw: [[1.0, 0.5] for _ in texts]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "error"),
    [
        ("not JSON: private content", "invalid_evidence"),
        (
            '{"transcription":"","description":"","uncertainties":"blurred"}',
            "no_searchable_content",
        ),
        (
            '{"transcription":"text","description":"","uncertainties":"","owner_email":"spoof"}',
            "invalid_evidence",
        ),
    ],
)
async def test_image_invalid_or_empty_evidence_fails_closed(monkeypatch, output, error):
    monkeypatch.setattr(
        "cognis.knowledgebase.image_extraction.get_attachment_analysis_route",
        AsyncMock(return_value=("vision-model", "vision-provider")),
    )
    with pytest.raises(ImageExtractionError, match=error) as exc:
        await extract_image(
            b"image",
            filename="photo.jpg",
            mime_type="image/jpeg",
            owner_email="owner@example.com",
            llm=_vision(output),
            session_factory=None,
        )
    assert "private content" not in str(exc.value)


@pytest.mark.asyncio
async def test_image_missing_route_capability_timeout_and_cancellation(monkeypatch):
    route = AsyncMock(return_value=(None, None))
    monkeypatch.setattr(
        "cognis.knowledgebase.image_extraction.get_attachment_analysis_route", route
    )
    llm = _vision()
    kwargs = dict(
        filename="photo.jpg",
        mime_type="image/jpeg",
        owner_email="owner@example.com",
        llm=llm,
        session_factory=None,
    )
    with pytest.raises(ImageExtractionError, match="requires_attachment_analysis_route"):
        await extract_image(b"image", **kwargs)
    route.return_value = ("vision-model", "vision-provider")
    llm.get_model_info.return_value.supports_vision = False
    with pytest.raises(ImageExtractionError, match="requires_vision_model"):
        await extract_image(b"image", **kwargs)
    llm.generate.assert_not_called()
    llm.get_model_info.return_value.supports_vision = True

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    llm.generate.side_effect = blocked
    with pytest.raises(ImageExtractionError, match="timeout"):
        await extract_image(b"image", timeout_seconds=0, **kwargs)
    llm.generate.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await extract_image(b"image", **kwargs)
    llm.generate.side_effect = RuntimeError("sensitive provider response")
    with pytest.raises(ImageExtractionError, match="analysis_failed:RuntimeError") as exc:
        await extract_image(b"image", **kwargs)
    assert "sensitive" not in str(exc.value)


@pytest.mark.asyncio
async def test_create_assign_ingest_image_search_and_original_provenance(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'images.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    image = io.BytesIO()
    Image.new("RGB", (16, 16), color="white").save(image, format="JPEG")
    original = image.getvalue()
    store = _ArtifactStore({"photo": original})
    llm = _vision()
    vectors = _VectorBackend()
    service = KnowledgebaseService(
        session_factory=factory,
        artifact_store=store,
        llm=llm,
        vector_backend=vectors,
        enabled=True,
    )
    deps = AgentManagementDependencies(
        session_factory=factory,
        caller_agent_id="creator",
        assignable_tools=[MANAGE_AGENTS_TOOL],
    )
    caller = KnowledgebaseAccessContext(
        actor_email="owner@example.com",
        agent_id="creator",
        agent_owner_email="owner@example.com",
    )
    try:
        async with factory() as session:
            session.add(User(email="owner@example.com", name="Owner", role="user"))
            session.add(
                Agent(
                    agent_id="creator",
                    owner_email="owner@example.com",
                    name="Creator",
                    status="active",
                    permissions={"allowed_knowledgebases": []},
                )
            )
            session.add(
                ArtifactRecordRow(
                    artifact_id="photo",
                    namespace="attachments",
                    object_id="photo",
                    filename="photo.jpeg",
                    mime_type="image/jpeg",
                    size_bytes=len(original),
                    owner_email="owner@example.com",
                    status="temporary",
                )
            )
            await upsert_model_routing(
                session,
                task_type="attachment_analysis",
                model="vision-model",
                provider_id=None,
                owner_email="owner@example.com",
            )
            await upsert_model_routing(
                session,
                task_type="embedding",
                model="embedding-model",
                provider_id=None,
            )
            await session.commit()
        kb = await service.create(
            owner_email="owner@example.com",
            name="Egypt",
            description=None,
            settings={},
            metadata_schema={},
            access_context=caller,
        )
        async with factory() as session:
            options = await assignable_knowledgebases(
                session,
                actor_email="owner@example.com",
                caller_agent_id="creator",
            )
            assert [row.knowledgebase_id for row in options] == [kb.knowledgebase_id]
        created = await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="creator",
            arguments={
                "action": "create",
                "name": "Travel",
                "assigned_knowledgebases": [kb.knowledgebase_id],
            },
        )
        target_id = created["agent"]["agent_id"]
        assert created["agent"]["status"] == "draft"
        loop = AgentLoop.__new__(AgentLoop)
        loop._session_factory = factory
        # The turn snapshot predates create. Discovery must use the current DB assignment.
        ctx = SimpleNamespace(
            session=SimpleNamespace(user_email="owner@example.com"),
            agent=SimpleNamespace(agent_id="creator", permissions=AgentPermissions()),
            tool_registry=SimpleNamespace(list_tools=lambda: [MANAGE_AGENTS_TOOL]),
        )
        resolved = await loop._resolve_introspection_dynamic_options(
            ctx, [MANAGE_AGENTS_TOOL, CALL_TOOL_TOOL]
        )
        descriptor = describe_available_tool(resolved, "manage_agents")["descriptor"]
        tools_set = next(op for op in descriptor["operations"] if op["operation"] == "tools_set")
        choices = {option["source"]: option["values"] for option in tools_set["dynamic_options"]}
        assert choices["agent_management.assignable_tools"] == ["builtin:manage_agents"]
        settings_op = next(
            op for op in descriptor["operations"] if op["operation"] == "settings_update"
        )
        assert any(
            "system:direct" in value["workflow_ids"]
            for option in settings_op["dynamic_options"]
            for value in option["values"]
        )
        kb_set = next(
            op for op in descriptor["operations"] if op["operation"] == "knowledgebases_set"
        )
        assert any(
            value["knowledgebase_id"] == kb.knowledgebase_id
            for option in kb_set["dynamic_options"]
            for value in option["values"]
        )
        await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="creator",
            arguments={
                "action": "update",
                "agent_id": target_id,
                "description": "Travel knowledge assistant",
            },
        )
        await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="creator",
            arguments={
                "action": "tools_set",
                "agent_id": target_id,
                "allow_tools": choices["agent_management.assignable_tools"],
            },
        )
        await handle_agent_management_action(
            deps=deps,
            actor_email="owner@example.com",
            current_agent_id="creator",
            arguments={
                "action": "knowledgebases_set",
                "agent_id": target_id,
                "knowledgebase_ids": [kb.knowledgebase_id],
            },
        )
        attachment = await service.attach(
            owner_email="owner@example.com",
            knowledgebase_id=kb.knowledgebase_id,
            artifact_id="photo",
            metadata={},
        )
        assert attachment is not None
        indexer = KnowledgebaseIndexer(
            session_factory=factory,
            artifact_store=store,
            llm=llm,
            vector_backend=vectors,
            enabled=True,
            poll_interval_seconds=0.01,
            max_artifact_size_bytes=1024,
            max_chunks_per_artifact=20,
            chunk_target_tokens=100,
            chunk_overlap_tokens=0,
            embedding_batch_size=2,
            controller_owner_id="image-test",
        )
        assert await indexer.run_once()
        assert not await indexer.run_once()
        llm.generate.assert_awaited_once()
        assert llm.generate.call_args.kwargs["acting_user_email"] == "owner@example.com"
        assert llm.generate.call_args.kwargs["task_type"] == "attachment_analysis"
        result = await service.search(
            owner_email="owner@example.com",
            knowledgebase_id=kb.knowledgebase_id,
            payload=KnowledgebaseSearchRequest(query="Hotel Dahab reservation"),
            access_context=caller,
        )
        assert result is not None and result.matches
        async with factory() as session:
            chunks = await list_knowledgebase_chunks(session, knowledgebase_id=kb.knowledgebase_id)
            assert any("Hotel Dahab" in chunk.text for chunk in chunks)
            assert all("vision-derived" in chunk.text for chunk in chunks)
            assert all(
                chunk.artifact_hash == hashlib.sha256(original).hexdigest() for chunk in chunks
            )
            assert all(
                chunk.locator["image_analysis"]["version"] == "vision_v1" for chunk in chunks
            )
            artifact = await session.get(ArtifactRecordRow, "photo")
            assert artifact.status == "attached" and artifact.expires_at is None
        citation = await service.source_context(
            owner_email="owner@example.com",
            knowledgebase_id=kb.knowledgebase_id,
            chunk_id=chunks[0].chunk_id,
            access_context=caller,
        )
        assert citation.artifact_id == "photo"
        assert "vision_derived_evidence_verify_original" in citation.warnings
        content = await service.document_content(
            owner_email="owner@example.com",
            knowledgebase_id=kb.knowledgebase_id,
            kb_artifact_id=attachment.kb_artifact_id,
            content_mode="extracted",
        )
        assert "Hotel Dahab" in content.text
        llm.generate.assert_awaited_once()
        assert store.content["photo"] == original
        # Fresh DB assignment reads also see revocation; owner alone is insufficient.
        async with factory() as session:
            creator = await session.get(Agent, "creator")
            creator.permissions = {"allowed_knowledgebases": []}
            await session.commit()
        with pytest.raises(AgentManagementError, match="Invalid knowledgebase_ids"):
            await handle_agent_management_action(
                deps=deps,
                actor_email="owner@example.com",
                current_agent_id="creator",
                arguments={
                    "action": "knowledgebases_set",
                    "agent_id": target_id,
                    "knowledgebase_ids": [kb.knowledgebase_id],
                },
            )
        assert (
            await service.search(
                owner_email="owner@example.com",
                knowledgebase_id=kb.knowledgebase_id,
                payload=KnowledgebaseSearchRequest(query="Dahab"),
                access_context=caller,
            )
            is None
        )
        async with factory() as session:
            indexed = await session.get(KnowledgebaseArtifactRow, attachment.kb_artifact_id)
            assert indexed.status == "indexed"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("mime_type", ["image/jpg", "image/jpeg; charset=binary", "IMAGE/JPEG"])
def test_image_admission_and_indexing_share_mime_normalization(mime_type):
    assert supports_artifact_type(filename="scan", mime_type=mime_type)
    assert image_mime_type(filename="scan", mime_type=mime_type) == "image/jpeg"


@pytest.mark.asyncio
async def test_image_content_rejects_generation_changed_between_reads(tmp_path, monkeypatch):
    engine, factory, vector = await _seed_replacement(tmp_path)
    monkeypatch.setattr(
        "cognis.knowledgebase.image_extraction.get_attachment_analysis_route",
        AsyncMock(return_value=("vision-model", None)),
    )
    llm = _vision()
    service = KnowledgebaseService(
        session_factory=factory,
        artifact_store=_ArtifactStore({}),
        llm=llm,
        vector_backend=vector,
        enabled=True,
    )
    worker = _indexer(factory, vector, llm)
    try:
        async with factory() as session:
            for artifact_id in ("artifact-old", "artifact-new"):
                artifact = await session.get(ArtifactRecordRow, artifact_id)
                artifact.mime_type = "image/jpeg"
                artifact.filename = "scan.jpg"
            await session.commit()

        async def publish_before_read(session, **kwargs):
            assert kwargs["generation"] == 1
            assert await worker.run_once()
            return await list_knowledgebase_chunks(session, **kwargs)

        monkeypatch.setattr(
            "cognis.knowledgebase.service.list_knowledgebase_chunks", publish_before_read
        )
        with pytest.raises(KnowledgebaseRequestError, match="generation changed"):
            await service.document_content(
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                kb_artifact_id="kba-1",
                content_mode="extracted",
            )
        monkeypatch.setattr(
            "cognis.knowledgebase.service.list_knowledgebase_chunks", list_knowledgebase_chunks
        )
        content = await service.document_content(
            owner_email="owner@example.com",
            knowledgebase_id="kb-1",
            kb_artifact_id="kba-1",
            content_mode="extracted",
        )
        assert content.artifact_id == "artifact-new"
        assert "Hotel Dahab" in content.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["detach", "supersede"])
async def test_late_vision_does_not_publish_after_attachment_mutation(
    tmp_path, monkeypatch, mutation
):
    engine, factory, vector = await _seed_replacement(tmp_path)
    monkeypatch.setattr(
        "cognis.knowledgebase.image_extraction.get_attachment_analysis_route",
        AsyncMock(return_value=("vision-model", None)),
    )
    llm = _vision()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return _vision().generate.return_value

    llm.generate.side_effect = blocked
    try:
        async with factory() as session:
            artifact = await session.get(ArtifactRecordRow, "artifact-new")
            artifact.mime_type = "image/jpeg"
            artifact.filename = "photo.jpg"
            await session.commit()
        worker = asyncio.create_task(_indexer(factory, vector, llm).run_once())
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as session:
            if mutation == "detach":
                await detach_knowledgebase_artifact(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-new",
                )
            else:
                await enqueue_knowledgebase_artifact_reindex(
                    session,
                    owner_email="owner@example.com",
                    knowledgebase_id="kb-1",
                    artifact_id="artifact-new",
                )
            await session.commit()
        release.set()
        assert await worker
        assert set(vector.points) == {"old-vector"}
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            assert attachment.active_generation == 1
            assert attachment.status == ("detached" if mutation == "detach" else "queued")
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_image_failure_retains_generation_and_retry_publishes_once(tmp_path, monkeypatch):
    engine, factory, vector = await _seed_replacement(tmp_path)
    monkeypatch.setattr(
        "cognis.knowledgebase.image_extraction.get_attachment_analysis_route",
        AsyncMock(return_value=("vision-model", None)),
    )
    llm = _vision("invalid private output")
    try:
        async with factory() as session:
            artifact = await session.get(ArtifactRecordRow, "artifact-new")
            artifact.mime_type = "image/jpeg"
            artifact.filename = "photo.jpg"
            await session.commit()
        worker = _indexer(factory, vector, llm)
        assert await worker.run_once()
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            assert attachment.active_generation == 1
            assert attachment.status == "stale"
            assert attachment.last_error == "knowledgebase_image_invalid_evidence"
            chunks = await list_knowledgebase_chunks(session, knowledgebase_id="kb-1")
            assert chunks[0].text == "old readable content"
            await enqueue_retry_knowledgebase_job(
                session,
                owner_email="owner@example.com",
                knowledgebase_id="kb-1",
                job_id=attachment.last_job_id,
            )
            await session.commit()
        llm.generate.return_value = _vision().generate.return_value
        assert await worker.run_once()
        async with factory() as session:
            attachment = await session.get(KnowledgebaseArtifactRow, "kba-1")
            assert attachment.status == "indexed"
            assert attachment.active_generation == attachment.desired_generation
            chunks = await list_knowledgebase_chunks(session, knowledgebase_id="kb-1")
            assert any("Hotel Dahab" in chunk.text for chunk in chunks)
        while await worker.run_once():
            pass  # Drain existing stale-vector cleanup without another vision request.
        assert llm.generate.await_count == 2
    finally:
        await engine.dispose()
