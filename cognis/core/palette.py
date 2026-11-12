"""Bounded, human-authorized metadata search for the global command palette."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import unicodedata
from collections import deque
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from cognis.core.project_identity import project_color
from cognis.store.library_queries import (
    content_origin_clause,
    discoverable_content_clause,
    human_content_clause,
    readable_projects,
)
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    Conversation,
    DeliverableRow,
    ProjectAgentRow,
    ProjectRow,
    Schedule,
    Task,
    User,
)
from cognis.store.palette_indexes import SOURCES
from cognis.store.project_queries import shared_conversation_clause
from cognis.store.queries import topic_conversation_clause, visible_agent_ids

Kind = Literal["agent", "project", "conversation", "task", "schedule", "deliverable", "artifact"]
MAX_PAGE = 40
RELEVANCE_QUERY_LENGTH = 3
DB_TIMEOUT_SECONDS = 10.0
OPERATION_TIMEOUT_SECONDS = 15.0


class InvalidPaletteCursor(ValueError):
    """The caller supplied an invalid or mismatched search continuation."""


class PaletteItem(BaseModel):
    """Small navigation projection; never contains payloads or private provenance."""

    kind: Kind
    id: str
    title: str
    project_id: str | None
    agent_id: str | None
    agent_name: str | None = None
    agent_avatar_url: str | None = None
    project_avatar_url: str | None = None
    project_icon: str | None = None
    project_color: str | None = None
    origin: Literal["agent", "user", "other"] | None = None
    producer_name: str | None = None
    status: str | None
    format: str | None
    updated_at: datetime
    href: str
    size_bytes: int = 0


class PalettePage(BaseModel):
    """Keyset continuation over indexed candidates, without a count query."""

    items: list[PaletteItem]
    next_cursor: str | None = None


def tokens(value: str) -> list[str]:
    """Bound query complexity before constructing native full-text expressions."""
    return re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)[:8]


def _plain(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value.casefold())
        if not unicodedata.combining(char)
    )


def _near(needle: str, word: str) -> bool:
    if word.startswith(needle):
        return True
    if len(needle) < 5 or abs(len(needle) - len(word)) > 2:
        return False
    previous = list(range(len(word) + 1))
    for i, left in enumerate(needle, 1):
        current = [i]
        for j, right in enumerate(word, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
        if min(current) > 2:
            return False
    return previous[-1] <= 2


def name_matches(title: str, query: str) -> bool:
    """Typo tolerance only over one bounded database candidate page."""
    words = re.findall(r"[^\W_]+", _plain(title), re.UNICODE)
    return all(any(_near(term, word) for word in words) for term in tokens(_plain(query)))


def _indexed(
    model: Any,
    kind: str,
    ref: Any,
    name: Any,
    terms: list[str],
    dialect: str,
    *,
    approximate: bool = True,
) -> Any:
    # Three-letter anchors permit bounded typo refinement without a similarity
    # scan over every name. Short queries use their full prefix.
    anchors = [term[:3] if approximate and len(term) >= 5 else term for term in terms]
    if dialect == "postgresql":
        expression = " & ".join(f"'{term}':*" for term in anchors)
        vector = sa.func.palette_name_vector(name)
        return vector.op("@@")(sa.func.to_tsquery(sa.literal_column("'simple'"), expression))
    table = next(table for source_kind, table, _, _ in SOURCES if source_kind == kind)
    index = f"palette_{table}"
    expression = " AND ".join(f'"{term}"*' for term in anchors)
    parameter = "palette_terms_anchor" if approximate else "palette_terms_exact"
    return ref.in_(
        sa.text(
            f"SELECT {ref.key} FROM {table} WHERE rowid IN "
            f"(SELECT rowid FROM {index} WHERE {index} MATCH :{parameter})"
        ).bindparams(**{parameter: expression})
    )


def _scope(user: str, query: str, kinds: list[Kind] | None) -> str:
    value = json.dumps(
        ["palette-v3", user, query, sorted(set(kinds)) if kinds is not None else None]
    )
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _decode(cursor: str | None, scope: str) -> tuple[int, datetime, str, str] | None:
    if not cursor:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor))
        if len(value) != 5 or value[0] != scope or value[1] not in range(700):
            raise ValueError
        if value[3] not in {source[0] for source in SOURCES} or not isinstance(value[4], str):
            raise ValueError
        return value[1], datetime.fromisoformat(value[2]), value[3], value[4]
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError) as exc:
        raise InvalidPaletteCursor("Invalid palette cursor") from exc


def _seek(rank: Any, updated: Any, kind: str, ref: Any, after: Any) -> Any:
    if after is None:
        return sa.true()
    score, timestamp, last_kind, last_id = after
    return sa.or_(
        rank > score,
        sa.and_(rank == score, updated < timestamp),
        sa.and_(
            rank == score,
            updated == timestamp,
            sa.or_(
                sa.literal(kind) > last_kind, sa.and_(sa.literal(kind) == last_kind, ref > last_id)
            ),
        ),
    )


def _task_access(model: Any, user_email: str, projects: Any) -> Any:
    return sa.or_(
        model.created_by == user_email,
        sa.and_(model.visibility == "project", model.project_id.in_(projects)),
    )


def _latest_task_run(user_email: str, projects: Any) -> Any:
    newer = aliased(Task)
    return sa.or_(
        Task.source_type != "scheduler",
        Task.source_ref.is_(None),
        Task.source_ref == "",
        ~sa.select(1)
        .where(
            newer.created_by == Task.created_by,
            newer.source_type == Task.source_type,
            newer.source_ref == Task.source_ref,
            _task_access(newer, user_email, projects),
            sa.tuple_(newer.created_at, newer.task_id) > sa.tuple_(Task.created_at, Task.task_id),
        )
        .exists(),
    )


async def search(
    session: AsyncSession,
    user_email: str,
    query: str,
    *,
    cursor: str | None = None,
    limit: int = 20,
    kinds: list[Kind] | None = None,
) -> PalettePage:
    """Execute a single bounded metadata union; authorization precedes every limit."""
    query = query.strip()[:160]
    limit = max(1, min(limit, MAX_PAGE))
    terms = tokens(_plain(query))
    if (query and not terms) or kinds == []:
        return PalettePage(items=[])
    # Blank and one- or two-letter queries cannot express relevance; they act as
    # a quick switcher over the most recently updated destinations of every kind.
    recent = len(query) < RELEVANCE_QUERY_LENGTH
    scope = _scope(user_email, query, kinds)
    after = _decode(cursor, scope)
    dialect = session.get_bind().dialect.name
    projects = readable_projects(user_email)
    specs = (
        (
            Agent,
            "agent",
            Agent.agent_id,
            sa.func.coalesce(Agent.name, sa.literal_column("''"))
            + sa.literal_column("' '")
            + sa.func.coalesce(Agent.display_name, sa.literal_column("''")),
            Agent.agent_id,
            sa.null(),
            sa.and_(
                Agent.agent_id.in_(visible_agent_ids(user_email)),
                Agent.agent_type == "primary",
                Agent.hidden.is_(False),
                Agent.is_system.is_(False),
                Agent.status == "active",
            ),
        ),
        (
            ProjectRow,
            "project",
            ProjectRow.project_id,
            ProjectRow.name,
            sa.null(),
            sa.null(),
            sa.and_(ProjectRow.project_id.in_(projects), ProjectRow.status == "active"),
        ),
        (
            Conversation,
            "conversation",
            Conversation.conversation_id,
            Conversation.title,
            Conversation.agent_id,
            sa.null(),
            sa.and_(
                Conversation.status != "deleted",
                topic_conversation_clause(),
                sa.or_(
                    Conversation.user_email == user_email, shared_conversation_clause(user_email)
                ),
            ),
        ),
        (
            Task,
            "task",
            Task.task_id,
            Task.title,
            Task.agent_id,
            sa.null(),
            sa.and_(
                _task_access(Task, user_email, projects), _latest_task_run(user_email, projects)
            ),
        ),
        (
            Schedule,
            "schedule",
            Schedule.schedule_id,
            Schedule.name,
            Schedule.agent_id,
            sa.null(),
            Schedule.created_by == user_email,
        ),
        (
            DeliverableRow,
            "deliverable",
            DeliverableRow.deliverable_id,
            DeliverableRow.title,
            DeliverableRow.owner_agent_id,
            DeliverableRow.format,
            sa.and_(
                human_content_clause(DeliverableRow, user_email),
                discoverable_content_clause(DeliverableRow),
            ),
        ),
        (
            ArtifactRecordRow,
            "artifact",
            ArtifactRecordRow.artifact_id,
            ArtifactRecordRow.filename,
            sa.case(
                (
                    content_origin_clause(ArtifactRecordRow) == "agent",
                    ArtifactRecordRow.owner_agent_id,
                ),
                else_=None,
            ),
            ArtifactRecordRow.mime_type,
            sa.and_(
                human_content_clause(ArtifactRecordRow, user_email),
                discoverable_content_clause(ArtifactRecordRow),
            ),
        ),
    )
    branches = []
    for model, kind, ref, name, agent, format_, accessible in specs:
        if kinds is not None and kind not in kinds:
            continue
        title = (
            sa.func.coalesce(sa.func.nullif(Agent.display_name, ""), Agent.name)
            if model is Agent
            else sa.func.coalesce(name, "Untitled")
        )
        origin = (
            content_origin_clause(model)
            if model in (DeliverableRow, ArtifactRecordRow)
            else sa.null()
        )
        rank: sa.ColumnElement[int] = sa.literal(0)
        if not recent:
            rank = sa.case(
                (sa.func.lower(title) == query.lower(), 0),
                (_indexed(model, kind, ref, name, terms, dialect, approximate=False), 20),
                else_=200,
            )
            # A word match anywhere outranks anchored typo candidates. Operational
            # destinations beat file floods within that band, before update time.
            priority = {
                "agent": 0,
                "project": 1,
                "task": 2,
                "schedule": 3,
                "conversation": 4,
                "deliverable": 5,
                "artifact": 6,
            }
            rank = (
                rank
                + priority[kind] * 4
                + (sa.case((origin == "agent", 0), else_=1) if model is ArtifactRecordRow else 0)
            )
            if model is Task:
                rank = rank + sa.case(
                    (Task.status.in_(["pending", "running", "waiting", "paused"]), 0), else_=1
                )
        if (
            not recent
            and kind in ("artifact", "deliverable")
            and (kinds is None or set(kinds) - {"artifact", "deliverable"})
        ):
            # Promote only a small content head when operational destinations
            # are enabled. Compute it before seek/limit so every remaining file
            # retains a stable rank and can still be reached through keysets.
            head = (
                sa.select(ref)
                .where(
                    accessible,
                    _indexed(model, kind, ref, name, terms, dialect) if terms else sa.true(),
                )
                .order_by(rank, model.updated_at.desc(), ref)
                .limit(2)
                .correlate(None)
            )
            rank = rank + sa.case((ref.in_(head), 0), else_=40)
        project_ref = model.__table__.c.get("project_id", sa.null())
        updated = model.updated_at
        stmt = sa.select(
            sa.literal(kind).label("kind"),
            ref.label("id"),
            title.label("title"),
            name.label("search_name"),
            sa.case((project_ref.in_(projects), project_ref), else_=None).label("project_id"),
            agent.label("agent_id"),
            origin.label("origin"),
            (
                DeliverableRow.owner_email
                if model is DeliverableRow
                else ArtifactRecordRow.owner_email
                if model is ArtifactRecordRow
                else sa.null()
            ).label("owner_email"),
            (
                sa.case((Schedule.enabled, "enabled"), else_="paused")
                if model is Schedule
                else model.__table__.c.status
            ).label("status"),
            format_.label("format"),
            (ArtifactRecordRow.size_bytes if model is ArtifactRecordRow else sa.literal(0)).label(
                "size_bytes"
            ),
            updated.label("updated_at"),
            rank.label("rank"),
        ).where(accessible, _seek(rank, updated, kind, ref, after))
        if terms:
            stmt = stmt.where(_indexed(model, kind, ref, name, terms, dialect))
        # A constant recent-mode rank stays out of the branch order so the
        # planner can stop at the newest accessible rows.
        order = (updated.desc(), ref) if recent else (rank, updated.desc(), ref)
        branches.append(stmt.order_by(*order).limit(limit + 1).subquery())
    rows = sa.union_all(*(sa.select(branch) for branch in branches)).subquery()
    page = (
        sa.select(rows)
        .order_by(rows.c.rank, rows.c.updated_at.desc(), rows.c.kind, rows.c.id)
        .limit(limit + 1)
        .subquery()
    )
    result = await session.execute(
        sa.select(
            page,
            sa.func.coalesce(sa.func.nullif(Agent.display_name, ""), Agent.name).label(
                "agent_name"
            ),
            sa.case(
                (
                    sa.and_(Agent.avatar_image_id.is_not(None), Agent.avatar_image_id != ""),
                    sa.literal("/api/v1/images/") + Agent.avatar_image_id,
                ),
                else_=Agent.avatar_url,
            ).label("agent_avatar_url"),
            sa.case(
                (page.c.origin == "user", sa.func.coalesce(User.name, User.email)),
                else_=None,
            ).label("producer_name"),
            ProjectRow.avatar_image_id.label("_project_avatar_image_id"),
            ProjectRow.avatar_url.label("_project_avatar_url"),
            ProjectRow.metadata_json["icon"].label("_project_icon"),
            ProjectRow.metadata_json["color"].label("_project_color"),
        )
        .outerjoin(User, User.email == page.c.owner_email)
        .outerjoin(
            Agent,
            sa.and_(
                Agent.agent_id == page.c.agent_id,
                sa.or_(
                    Agent.owner_email == user_email,
                    Agent.agent_id.in_(visible_agent_ids(user_email)),
                    Agent.agent_id.in_(
                        sa.select(ProjectAgentRow.agent_id).where(
                            ProjectAgentRow.project_id.in_(projects)
                        )
                    ),
                ),
            ),
        )
        .outerjoin(
            ProjectRow,
            sa.and_(
                page.c.kind == "project",
                ProjectRow.project_id == page.c.id,
                ProjectRow.project_id.in_(projects),
            ),
        )
        .order_by(page.c.rank, page.c.updated_at.desc(), page.c.kind, page.c.id)
    )
    candidates = list(result.mappings())
    scanned = candidates[:limit]
    items = []
    for row in scanned:
        if terms and not name_matches(row["search_name"] or row["title"], query):
            continue
        kind = row["kind"]
        image_id = row["_project_avatar_image_id"]
        icon = row["_project_icon"]
        route = {"conversation": "chat", "task": "tasks", "schedule": "schedules"}.get(kind)
        href = (
            f"/projects/{row['id']}?tab=overview"
            if kind == "project"
            else "/chat?" + urlencode({"agent_id": row["id"], "scope": "agent_direct"})
            if kind == "agent"
            else f"/{route}/{row['id']}"
            if route
            else f"/library/{kind}/{row['id']}"
        )
        items.append(
            PaletteItem(
                **{
                    key: value
                    for key, value in row.items()
                    if key not in ("rank", "owner_email", "search_name")
                    and not key.startswith("_project_")
                },
                project_avatar_url=(
                    f"/api/v1/images/{quote(image_id, safe='')}"
                    if image_id
                    else row["_project_avatar_url"]
                ),
                project_icon=icon if isinstance(icon, str) and len(icon) <= 64 else None,
                project_color=(
                    project_color(row["id"], {"color": row["_project_color"]})
                    if kind == "project"
                    else None
                ),
                href=href,
            )
        )
    next_cursor = None
    if len(candidates) > limit and scanned:
        last = scanned[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                [scope, last["rank"], last["updated_at"].isoformat(), last["kind"], last["id"]]
            ).encode()
        ).decode()
    return PalettePage(items=items, next_cursor=next_cursor)


class PaletteAdmission:
    """Per-controller admission without a waiter queue or unbounded per-user state."""

    def __init__(self, *, per_user_rate: int = 8, global_rate: int = 24) -> None:
        self.active: set[str] = set()
        self.limit = 4
        self.per_user_rate = per_user_rate
        self.global_rate = global_rate
        self.starts: deque[tuple[str, float]] = deque()

    async def run(self, session: AsyncSession, user: str, query: str, **kwargs: Any) -> PalettePage:
        """Allow one query per user and four per controller; enforce a DB deadline."""
        now = time.monotonic()
        while self.starts and self.starts[0][1] <= now - 1:
            self.starts.popleft()
        if (
            user in self.active
            or len(self.active) >= self.limit
            or len(self.starts) >= self.global_rate
            or sum(owner == user for owner, _ in self.starts) >= self.per_user_rate
        ):
            raise BlockingIOError("Palette search busy")
        self.starts.append((user, now))
        self.active.add(user)
        sqlite_driver = None
        raw = None
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                if session.get_bind().dialect.name == "postgresql":
                    await session.execute(
                        sa.text(
                            f"SET LOCAL statement_timeout = '{int(DB_TIMEOUT_SECONDS * 1000)}ms'"
                        )
                    )
                else:
                    connection = await session.connection()
                    raw = await connection.get_raw_connection()
                    sqlite_driver = raw.driver_connection
                    if sqlite_driver is None:
                        raise RuntimeError("SQLite driver unavailable")
                    deadline = time.monotonic() + DB_TIMEOUT_SECONDS
                    await sqlite_driver.set_progress_handler(
                        lambda: int(time.monotonic() > deadline), 1000
                    )
                return await search(session, user, query, **kwargs)
        finally:
            try:
                # Cancellation can already have invalidated and closed this driver.
                if sqlite_driver is not None and raw is not None and raw.is_valid:
                    try:
                        await sqlite_driver.set_progress_handler(None, 0)
                    except BaseException:
                        # Never return a connection with an armed deadline to the pool.
                        await session.invalidate()
                        raise
            finally:
                self.active.discard(user)
