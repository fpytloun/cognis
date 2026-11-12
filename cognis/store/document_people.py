"""Human-only document grants; agent-runtime read predicates do not consume them."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.document_visibility import unmanaged_deliverable_clause
from cognis.store.models import ArtifactRecordRow, DeliverableRow, DocumentUserGrant, User


def user_grant_clause(model: Any, user_email: str) -> Any:
    """Correlate a grant with the content's lineage and immutable owner."""
    kind = "deliverable" if model is DeliverableRow else "artifact"
    key = model.deliverable_id if model is DeliverableRow else model.artifact_id
    grant = sa.exists().where(
        DocumentUserGrant.kind == kind,
        DocumentUserGrant.lineage_id == sa.func.coalesce(model.lineage_id, key),
        DocumentUserGrant.owner_email == model.owner_email,
        DocumentUserGrant.principal_email == user_email,
        DocumentUserGrant.revoked.is_(False),
        sa.exists().where(User.email == user_email, User.is_active.is_(True)),
    )
    return sa.and_(grant, unmanaged_deliverable_clause(model)) if model is DeliverableRow else grant


async def direct_grant(db: AsyncSession, row: Any, email: str) -> DocumentUserGrant | None:
    """Return a live human grant without granting access to its producing context."""
    model = type(row)
    if model not in (ArtifactRecordRow, DeliverableRow):
        return None
    key = row.deliverable_id if model is DeliverableRow else row.artifact_id
    result = await db.scalars(
        sa.select(DocumentUserGrant).where(
            DocumentUserGrant.kind == ("deliverable" if model is DeliverableRow else "artifact"),
            DocumentUserGrant.lineage_id == (row.lineage_id or key),
            DocumentUserGrant.owner_email == row.owner_email,
            DocumentUserGrant.principal_email == email,
            DocumentUserGrant.revoked.is_(False),
            sa.exists().where(User.email == email, User.is_active.is_(True)),
            unmanaged_deliverable_clause(row) if model is DeliverableRow else sa.true(),
        )
    )
    return result.one_or_none()


async def person_may_read_document(db: AsyncSession, row: Any, email: str) -> bool:
    """Use the canonical human Library predicate, never the agent-access predicate."""
    from cognis.store.library_queries import human_content_clause

    if row.owner_email == email:
        return True
    model = type(row)
    if model not in (ArtifactRecordRow, DeliverableRow):
        return False
    key = model.deliverable_id if model is DeliverableRow else model.artifact_id
    ref = row.deliverable_id if model is DeliverableRow else row.artifact_id
    return (
        await db.scalar(sa.select(key).where(key == ref, human_content_clause(model, email)))
        is not None
    )
