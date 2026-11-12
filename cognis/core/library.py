"""Paginated Library application boundary for authenticated human readers."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.document_favorites import favorite_expression
from cognis.store.library_queries import library_items


class LibraryFilters(BaseModel):
    """Metadata filters shared by flat and lazy grouped browsing."""

    kind: Literal["deliverable", "artifact"] = "deliverable"
    q: str = Field(default="", max_length=200)
    project_id: str | None = None
    conversation_id: str | None = None
    source_id: str | None = None
    agent_id: str | None = None
    origin: Literal["all", "user", "agent", "other"] = "all"
    ownership: Literal["all", "mine", "shared"] = "all"
    format: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    sort: Literal["updated", "created"] = "updated"
    favorites: bool = False


class LibraryItem(BaseModel):
    """List/detail metadata; payloads are fetched separately by exact reference."""

    id: str
    kind: Literal["deliverable", "artifact"]
    title: str | None = None
    format: str
    owner_email: str | None = None
    author_email: str | None = None
    author_agent_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    task_id: str | None = None
    task_title: str | None = None
    source_id: str | None = None
    source_title: str | None = None
    lineage_id: str | None = None
    revision: int = 1
    created_at: datetime
    updated_at: datetime
    size_bytes: int = 0
    status: str
    origin: Literal["user", "agent", "other"]
    is_readonly: bool = False
    is_favorite: bool = False
    # Open, visible comments on any revision of the document; owner-only.
    open_comments: int = 0


class LibraryPage(BaseModel):
    """A deterministic keyset page of accessible content."""

    items: list[LibraryItem]
    next_cursor: str | None = None


class LibraryGroup(BaseModel):
    """One folder with a complete matching-item count."""

    id: str
    label: str
    count: int


class LibraryGroupsPage(BaseModel):
    """A paginated set of folders, not a grouping of a truncated item page."""

    groups: list[LibraryGroup]
    next_cursor: str | None = None


def _scope(user_email: str, filters: LibraryFilters, group_by: str = "") -> str:
    return hashlib.sha256(
        f"{user_email}:{filters.model_dump_json()}:{group_by}".encode()
    ).hexdigest()


def _cursor(scope: str, values: list[str]) -> str:
    return base64.urlsafe_b64encode(json.dumps([scope, values]).encode()).decode()


def _decode(cursor: str | None, scope: str) -> list[str] | None:
    if not cursor:
        return None
    try:
        stored_scope, values = json.loads(base64.urlsafe_b64decode(cursor))
        if (
            stored_scope != scope
            or not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
        ):
            raise ValueError
        return values
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid cursor for these filters") from exc


def _filtered(user_email: str, filters: LibraryFilters) -> Any:
    rows = library_items(user_email, filters.kind)
    favorite = favorite_expression(
        user_email, filters.kind, sa.func.coalesce(rows.c.lineage_id, rows.c.id)
    )
    stmt = sa.select(rows, favorite.label("is_favorite"))
    if filters.favorites:
        stmt = stmt.where(favorite)
    for name in ("project_id", "conversation_id", "source_id"):
        value = getattr(filters, name)
        if value is not None:
            stmt = stmt.where(rows.c[name].is_(None) if value == "" else rows.c[name] == value)
    for name in ("agent_id", "format"):
        if value := getattr(filters, name):
            stmt = stmt.where(rows.c[name] == value)
    if filters.origin != "all":
        stmt = stmt.where(rows.c.origin == filters.origin)
    if filters.ownership == "mine":
        stmt = stmt.where(rows.c.owner_email == user_email)
    elif filters.ownership == "shared":
        stmt = stmt.where(rows.c.owner_email != user_email)
    if filters.after:
        stmt = stmt.where(rows.c.created_at >= filters.after)
    if filters.before:
        stmt = stmt.where(rows.c.created_at <= filters.before)
    for token in filters.q.strip().split():
        escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(sa.func.lower(rows.c.title).like(f"%{escaped.lower()}%", escape="\\"))
    return stmt.subquery()


async def _attach_open_comments(
    session: AsyncSession, user_email: str, items: list[LibraryItem]
) -> None:
    """Count open comments per owned document in one query, across all its revisions."""
    counts = await _open_comments_by_lineage(
        session, user_email, {item.lineage_id or item.id for item in items if not item.is_readonly}
    )
    for item in items:
        if not item.is_readonly:
            item.open_comments = counts.get(item.lineage_id or item.id, 0)


async def _open_comments_by_lineage(
    session: AsyncSession, user_email: str, lineages: set[str]
) -> dict[str, int]:
    from cognis.store.feedback_models import FeedbackSubjectRow, FeedbackThreadRow

    if not lineages:
        return {}
    result = await session.execute(
        sa.select(FeedbackSubjectRow.lineage_id, sa.func.count(FeedbackThreadRow.thread_id))
        .join(FeedbackThreadRow, FeedbackThreadRow.subject_id == FeedbackSubjectRow.subject_id)
        .where(
            FeedbackSubjectRow.owner_email == user_email,
            FeedbackSubjectRow.lineage_id.in_(lineages),
            FeedbackThreadRow.state == "open",
            FeedbackThreadRow.hidden.is_(False),
        )
        .group_by(FeedbackSubjectRow.lineage_id)
    )
    return {str(lineage): int(count) for lineage, count in result.tuples().all()}


async def document_comment_counts(
    session: AsyncSession, user_email: str, refs: list[str]
) -> dict[str, int]:
    """Read owner-only counts for a bounded batch of exact document references.

    Shared, missing and unavailable documents all return zero; neither document
    existence nor somebody else's private review metadata is disclosed.
    """
    from cognis.store.library_queries import discoverable_content_clause
    from cognis.store.models import ArtifactRecordRow, DeliverableRow

    counts = dict.fromkeys(refs, 0)
    if not refs:
        return counts
    records = (
        (
            await session.execute(
                sa.union_all(
                    sa.select(
                        ArtifactRecordRow.artifact_id,
                        sa.func.coalesce(
                            ArtifactRecordRow.lineage_id, ArtifactRecordRow.artifact_id
                        ),
                    ).where(
                        ArtifactRecordRow.artifact_id.in_(refs),
                        ArtifactRecordRow.owner_email == user_email,
                        discoverable_content_clause(ArtifactRecordRow, exact=True),
                    ),
                    sa.select(
                        DeliverableRow.deliverable_id,
                        sa.func.coalesce(DeliverableRow.lineage_id, DeliverableRow.deliverable_id),
                    ).where(
                        DeliverableRow.deliverable_id.in_(refs),
                        DeliverableRow.owner_email == user_email,
                    ),
                )
            )
        )
        .tuples()
        .all()
    )
    by_lineage = await _open_comments_by_lineage(
        session, user_email, {str(lineage) for _, lineage in records}
    )
    for ref, lineage in records:
        counts[ref] = by_lineage.get(lineage, 0)
    return counts


async def browse(
    session: AsyncSession,
    user_email: str,
    filters: LibraryFilters,
    *,
    cursor: str | None = None,
    limit: int = 40,
) -> LibraryPage:
    """List authorized metadata with access applied before pagination."""
    rows = _filtered(user_email, filters)
    timestamp = rows.c.updated_at if filters.sort == "updated" else rows.c.created_at
    scope = _scope(user_email, filters)
    stmt = sa.select(rows)
    if values := _decode(cursor, scope):
        if len(values) != 2:
            raise ValueError("Invalid item cursor")
        date = datetime.fromisoformat(values[0])
        stmt = stmt.where(
            sa.or_(timestamp < date, sa.and_(timestamp == date, rows.c.id < values[1]))
        )
    records = (
        (await session.execute(stmt.order_by(timestamp.desc(), rows.c.id.desc()).limit(limit + 1)))
        .mappings()
        .all()
    )
    items = [
        LibraryItem(**dict(row), is_readonly=row["owner_email"] != user_email)
        for row in records[:limit]
    ]
    await _attach_open_comments(session, user_email, items)
    next_cursor = None
    if len(records) > limit and items:
        last = items[-1]
        date = last.updated_at if filters.sort == "updated" else last.created_at
        next_cursor = _cursor(scope, [date.isoformat(), last.id])
    return LibraryPage(items=items, next_cursor=next_cursor)


async def groups(
    session: AsyncSession,
    user_email: str,
    filters: LibraryFilters,
    *,
    group_by: Literal["project", "conversation", "agent", "format"],
    cursor: str | None = None,
    limit: int = 40,
) -> LibraryGroupsPage:
    """List lazy folder nodes over all matching readable items."""
    rows = _filtered(user_email, filters)
    key = sa.func.coalesce(
        rows.c.source_id
        if group_by == "conversation"
        else rows.c.format
        if group_by == "format"
        else rows.c[f"{group_by}_id"],
        "",
    )
    label_column = {
        "project": rows.c.project_name,
        "conversation": rows.c.source_title,
        "agent": rows.c.agent_name,
        "format": rows.c.format,
    }[group_by]
    label = sa.func.coalesce(sa.func.max(label_column), f"No {group_by}")
    timestamp = sa.func.max(rows.c.updated_at if filters.sort == "updated" else rows.c.created_at)
    stmt = sa.select(
        key.label("id"),
        label.label("label"),
        sa.func.count().label("count"),
        timestamp.label("sort_at"),
    ).group_by(key)
    scope = _scope(user_email, filters, group_by)
    if values := _decode(cursor, scope):
        if len(values) != 2:
            raise ValueError("Invalid folder cursor")
        date = datetime.fromisoformat(values[0])
        stmt = stmt.having(sa.or_(timestamp < date, sa.and_(timestamp == date, key < values[1])))
    records = (
        (await session.execute(stmt.order_by(timestamp.desc(), key.desc()).limit(limit + 1)))
        .mappings()
        .all()
    )
    result = [LibraryGroup(**dict(row)) for row in records[:limit]]
    return LibraryGroupsPage(
        groups=result,
        next_cursor=_cursor(scope, [records[limit - 1]["sort_at"].isoformat(), result[-1].id])
        if len(records) > limit and result
        else None,
    )


async def item(
    session: AsyncSession,
    user_email: str,
    kind: str,
    ref: str,
) -> LibraryItem | None:
    """Resolve an exact, independently authorized content reference."""
    rows = library_items(user_email, kind, exact=True, ref_id=ref)
    favorite = favorite_expression(user_email, kind, sa.func.coalesce(rows.c.lineage_id, rows.c.id))
    row = (
        (
            await session.execute(
                sa.select(rows, favorite.label("is_favorite")).where(rows.c.id == ref)
            )
        )
        .mappings()
        .first()
    )
    return LibraryItem(**dict(row), is_readonly=row["owner_email"] != user_email) if row else None
