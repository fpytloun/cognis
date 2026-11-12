"""Transaction-local staging transitions; callers own authorization and commit."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store import feedback_models as rows

from .primitives import FeedbackError, affected


async def delivered_versions(db: AsyncSession, version_ids: Sequence[str]) -> set[str]:
    """Return the entry versions already sent as instructions in an accepted submission."""
    if not version_ids:
        return set()
    found = await db.scalars(
        select(rows.FeedbackSubmissionItemRow.entry_version_id)
        .join(
            rows.FeedbackSubmissionRow,
            rows.FeedbackSubmissionRow.submission_id
            == rows.FeedbackSubmissionItemRow.submission_id,
        )
        .where(
            rows.FeedbackSubmissionRow.delivery == "accepted",
            rows.FeedbackSubmissionItemRow.role == "instruction",
            rows.FeedbackSubmissionItemRow.entry_version_id.in_(list(version_ids)),
        )
    )
    return set(found.all())


async def stage_entry(
    db: AsyncSession,
    *,
    owner_email: str,
    conversation_id: str,
    entry_id: str,
    entry_version_id: str,
    subject_id: str,
    anchor_id: str,
    expected_version: int | None,
    inclusion: Literal["prepared", "included"] = "prepared",
) -> rows.FeedbackStagingItemRow:
    """Prepare authored versions (None), or apply an explicit item-version CAS.

    Fence the entry before item/scope writes, matching admission's lock order.
    No transition grants access, includes a first item, or dispatches work.
    """
    fenced = await db.execute(
        update(rows.FeedbackEntryRow)
        .where(
            rows.FeedbackEntryRow.entry_id == entry_id,
            rows.FeedbackEntryRow.head_version_id == entry_version_id,
        )
        .values(head_version_id=entry_version_id)
    )
    if affected(fenced) != 1:
        raise FeedbackError(409, "version_conflict", "Select the current feedback version")
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(rows.FeedbackStagingScopeRow)
        .values(owner_email=owner_email, conversation_id=conversation_id, version=0)
        .on_conflict_do_nothing(index_elements=["owner_email", "conversation_id"])
    )
    item = await db.get(
        rows.FeedbackStagingItemRow,
        (owner_email, conversation_id, entry_id),
        populate_existing=True,
    )
    if item is None:
        if expected_version not in (None, 0):
            raise FeedbackError(409, "version_conflict", "Staged feedback changed")
        item = rows.FeedbackStagingItemRow(
            owner_email=owner_email,
            conversation_id=conversation_id,
            entry_id=entry_id,
            entry_version_id=entry_version_id,
            subject_id=subject_id,
            anchor_id=anchor_id,
            version=1,
            inclusion="prepared",
        )
        # First staging is never inclusion, including an explicit first PUT.
        db.add(item)
        await db.flush()
    else:
        if expected_version is None:
            if item.entry_version_id == entry_version_id:
                return item
            expected_version = item.version
            inclusion = "prepared"
        changed = await db.execute(
            update(rows.FeedbackStagingItemRow)
            .where(
                rows.FeedbackStagingItemRow.owner_email == owner_email,
                rows.FeedbackStagingItemRow.conversation_id == conversation_id,
                rows.FeedbackStagingItemRow.entry_id == entry_id,
                rows.FeedbackStagingItemRow.version == expected_version,
            )
            .values(
                entry_version_id=entry_version_id,
                version=expected_version + 1,
                inclusion=inclusion,
                subject_id=subject_id,
                anchor_id=anchor_id,
            )
        )
        if affected(changed) != 1:
            raise FeedbackError(409, "version_conflict", "Staged feedback changed")
        await db.refresh(item)
    await db.execute(
        update(rows.FeedbackStagingScopeRow)
        .where(
            rows.FeedbackStagingScopeRow.owner_email == owner_email,
            rows.FeedbackStagingScopeRow.conversation_id == conversation_id,
        )
        .values(version=rows.FeedbackStagingScopeRow.version + 1)
    )
    return item
