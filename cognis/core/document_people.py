"""Owner-controlled sharing with signed-in people across a document lineage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.document_sharing import owned_document
from cognis.core.feedback.primitives import FeedbackError, affected
from cognis.store.document_visibility import unmanaged_deliverable_clause
from cognis.store.models import DocumentUserGrant, User

DocumentKind = Literal["artifact", "deliverable"]


class PersonGrantRequest(BaseModel):
    """Explicit desired access plus the version the owner reviewed."""

    model_config = ConfigDict(extra="forbid")
    principal_email: str = Field(min_length=3, max_length=320)
    expected_version: int = Field(ge=0)
    allow_comments: bool = False
    revoked: bool = False


class PersonGrant(BaseModel):
    """Revoked records retain their versions to prevent revoke/regrant ABA."""

    principal_email: str
    allow_comments: bool
    version: int
    revoked: bool


class PeoplePage(BaseModel):
    kind: DocumentKind
    lineage_id: str
    items: list[PersonGrant]


async def people(db: AsyncSession, email: str, kind: DocumentKind, ref: str) -> PeoplePage:
    """Only the owner can enumerate direct recipients."""
    row = await owned_document(db, kind, ref, email)
    if kind == "deliverable" and not await db.scalar(sa.select(unmanaged_deliverable_clause(row))):
        raise LookupError("Document not found")
    lineage = row.lineage_id or ref
    grants = (
        await db.scalars(
            sa.select(DocumentUserGrant)
            .where(
                DocumentUserGrant.kind == kind,
                DocumentUserGrant.lineage_id == lineage,
                DocumentUserGrant.owner_email == email,
            )
            .order_by(DocumentUserGrant.principal_email)
        )
    ).all()
    return PeoplePage(
        kind=kind,
        lineage_id=lineage,
        items=[
            PersonGrant(
                principal_email=item.principal_email,
                allow_comments=item.allow_comments,
                version=item.version,
                revoked=item.revoked,
            )
            for item in grants
        ],
    )


async def set_person(
    db: AsyncSession, email: str, kind: DocumentKind, ref: str, request: PersonGrantRequest
) -> PeoplePage:
    """Serialize with document edits; CAS fences concurrent grants and revocation."""
    row = await owned_document(db, kind, ref, email, lock=True)
    if kind == "deliverable" and not await db.scalar(sa.select(unmanaged_deliverable_clause(row))):
        raise LookupError("Document not found")
    principal = request.principal_email.strip().lower()
    user = await db.get(User, principal)
    if principal == email or (not request.revoked and (user is None or not user.is_active)):
        raise FeedbackError(422, "invalid_recipient", "Choose another active Cognis user")
    values = dict(
        kind=kind,
        lineage_id=row.lineage_id or ref,
        principal_email=principal,
        owner_email=email,
        allow_comments=request.allow_comments,
        revoked=request.revoked,
        updated_at=datetime.now(UTC),
    )
    if request.expected_version == 0:
        if request.revoked:
            raise FeedbackError(409, "version_conflict", "Reload sharing before changing access")
        insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        result = await db.execute(
            insert(DocumentUserGrant).values(**values, version=1).on_conflict_do_nothing()
        )
    else:
        result = await db.execute(
            sa.update(DocumentUserGrant)
            .where(
                DocumentUserGrant.kind == kind,
                DocumentUserGrant.lineage_id == values["lineage_id"],
                DocumentUserGrant.principal_email == principal,
                DocumentUserGrant.owner_email == email,
                DocumentUserGrant.version == request.expected_version,
            )
            .values(
                allow_comments=request.allow_comments,
                revoked=request.revoked,
                updated_at=values["updated_at"],
                version=request.expected_version + 1,
            )
        )
    if affected(result) != 1:
        raise FeedbackError(
            409, "version_conflict", "Access changed; reload sharing before retrying"
        )
    await db.commit()
    return await people(db, email, kind, ref)
