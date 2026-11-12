"""Canonical output metadata for the already-authorized Work page."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.chat_v2.schemas import WorkProjectionResponse
from cognis.store.models import ArtifactRecordRow


async def hydrate_work_artifact_metadata(
    db: AsyncSession, projection: WorkProjectionResponse, *, owner_email: str
) -> None:
    """Add exact revision facts without replacing message-bound artifact IDs."""

    if not projection.artifacts:
        return
    rows = (
        await db.scalars(
            select(ArtifactRecordRow).where(
                ArtifactRecordRow.artifact_id.in_(
                    [item.artifact_id for item in projection.artifacts]
                ),
                ArtifactRecordRow.owner_email == owner_email,
                ArtifactRecordRow.deleted_at.is_(None),
            )
        )
    ).all()
    by_id = {row.artifact_id: row for row in rows}
    lineage = func.coalesce(ArtifactRecordRow.lineage_id, ArtifactRecordRow.artifact_id)
    latest = dict(
        (
            await db.execute(
                select(lineage, func.max(ArtifactRecordRow.revision))
                .where(
                    lineage.in_([row.lineage_id or row.artifact_id for row in rows]),
                    ArtifactRecordRow.owner_email == owner_email,
                    ArtifactRecordRow.deleted_at.is_(None),
                )
                .group_by(lineage)
            )
        )
        .tuples()
        .all()
    )
    for item in projection.artifacts:
        row = by_id.get(item.artifact_id)
        if row is None:
            continue
        item.lineage_id = row.lineage_id or row.artifact_id
        item.revision = row.revision
        item.latest_revision = latest.get(item.lineage_id, row.revision)
        item.created_at = row.created_at.isoformat()
        # Revisions are immutable; lifecycle bookkeeping does not change content.
        item.updated_at = item.created_at
