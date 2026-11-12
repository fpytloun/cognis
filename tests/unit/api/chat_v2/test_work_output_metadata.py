from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cognis.api.chat_v2.schemas import (
    TimelineScope,
    WorkArtifact,
    WorkProjectionResponse,
    WorkSummary,
)
from cognis.api.chat_v2.work_output_metadata import hydrate_work_artifact_metadata
from cognis.store.models import ArtifactRecordRow


@pytest.mark.asyncio
async def test_work_artifact_metadata_preserves_exact_refs_and_reports_latest_live_revision() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(ArtifactRecordRow.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    async with factory() as db:
        for artifact_id, revision, owner, deleted in [
            ("art_first", 1, "owner@example.com", False),
            ("art_fourth", 4, "owner@example.com", False),
            ("art_deleted", 5, "owner@example.com", True),
            ("art_foreign", 9, "other@example.com", False),
        ]:
            db.add(
                ArtifactRecordRow(
                    artifact_id=artifact_id,
                    namespace="artifacts",
                    object_id=artifact_id,
                    filename="report.csv",
                    owner_email=owner,
                    mime_type="text/csv",
                    status="attached",
                    lineage_id="art_first",
                    revision=revision,
                    created_at=created,
                    updated_at=created + timedelta(hours=2),
                    deleted_at=created if deleted else None,
                )
            )
        await db.commit()
        projection = WorkProjectionResponse(
            projection_version="chat-v2",
            scope=TimelineScope(
                key="conversation:c-main", kind="conversation", conversation_id="c-main"
            ),
            summary=WorkSummary(mutations=0, commands=0, changed_files=0, artifacts=2),
            server_time=created.isoformat(),
            artifacts=[
                WorkArtifact(artifact_id="art_first", filename="report.csv"),
                WorkArtifact(artifact_id="art_foreign", filename="foreign.csv"),
            ],
        )
        await hydrate_work_artifact_metadata(db, projection, owner_email="owner@example.com")

    first, foreign = projection.artifacts
    assert first.artifact_id == "art_first"
    assert first.revision == 1
    assert first.latest_revision == 4
    assert first.lineage_id == "art_first"
    assert first.created_at is not None
    assert datetime.fromisoformat(first.created_at).replace(tzinfo=UTC) == created
    assert first.updated_at == first.created_at
    assert foreign.revision is None
    await engine.dispose()
