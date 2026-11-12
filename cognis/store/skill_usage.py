"""Content-free per-agent skill usage counters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import AgentSkillUsageRow

UNUSED_ATTACHED_SKILL_DAYS = 30
REPEATED_SEARCH_LOADS = 3


def _insert(session: AsyncSession) -> Any:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert(AgentSkillUsageRow)
    if dialect == "sqlite":
        return sqlite_insert(AgentSkillUsageRow)
    raise ValueError(f"Unsupported skill usage database dialect: {dialect}")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def record_skill_search_hits(
    session: AsyncSession, agent_id: str, skill_ids: Sequence[str], *, at: datetime | None = None
) -> None:
    """Mark skills returned by ``skill_search``; the caller commits."""

    hit_at = at or datetime.now(UTC)
    for skill_id in dict.fromkeys(skill_ids):
        statement = _insert(session).values(
            agent_id=agent_id, skill_id=skill_id, last_search_hit_at=hit_at, created_at=hit_at
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["agent_id", "skill_id"],
                set_={"last_search_hit_at": statement.excluded.last_search_hit_at},
            )
        )


async def record_skill_load(
    session: AsyncSession, agent_id: str, skill_id: str, *, at: datetime | None = None
) -> None:
    """Count one load; it came from search when a hit is newer than the last load."""

    loaded_at = at or datetime.now(UTC)
    statement = _insert(session).values(
        agent_id=agent_id,
        skill_id=skill_id,
        load_count=1,
        search_load_count=0,
        last_loaded_at=loaded_at,
        created_at=loaded_at,
    )
    row = AgentSkillUsageRow
    found_by_search = (row.last_search_hit_at.is_not(None)) & (
        row.last_loaded_at.is_(None) | (row.last_search_hit_at > row.last_loaded_at)
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["agent_id", "skill_id"],
            set_={
                "load_count": row.load_count + 1,
                "search_load_count": row.search_load_count + case((found_by_search, 1), else_=0),
                "last_loaded_at": statement.excluded.last_loaded_at,
            },
        )
    )


async def list_agent_skill_usage(
    session: AsyncSession, agent_id: str
) -> dict[str, AgentSkillUsageRow]:
    """Return usage rows for one agent keyed by skill id."""

    rows = await session.scalars(
        select(AgentSkillUsageRow).where(AgentSkillUsageRow.agent_id == agent_id)
    )
    return {row.skill_id: row for row in rows}


async def skill_usage_tracking_started(session: AsyncSession) -> datetime | None:
    """Return when usage counting first observed any agent, if ever."""

    return _utc(await session.scalar(select(func.min(AgentSkillUsageRow.created_at))))


def skill_usage_payload(
    row: AgentSkillUsageRow | None,
    *,
    assignment: str,
    agent_updated_at: datetime,
    tracking_started: datetime | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Serialize usage with a curation suggestion; nothing is changed automatically.

    ``assignment`` is ``attached``, ``unattached``, ``global`` (attached to all
    agents), or ``auto_loaded``; only the first two receive suggestions. Attach
    time is not stored, so the agent's last update bounds it: an attached skill
    counts as unused only when neither that update, its last load, nor the start
    of usage counting falls within the window.
    """

    current = now or datetime.now(UTC)
    last_loaded_at = _utc(row.last_loaded_at) if row else None
    suggestion: str | None = None
    if assignment == "attached":
        observed_from = last_loaded_at or (_utc(row.created_at) if row else tracking_started)
        if observed_from is not None and current - max(
            observed_from, _utc(agent_updated_at) or observed_from
        ) >= timedelta(days=UNUSED_ATTACHED_SKILL_DAYS):
            suggestion = "consider_removing"
    elif (
        assignment == "unattached"
        and row is not None
        and row.search_load_count >= REPEATED_SEARCH_LOADS
    ):
        suggestion = "consider_attaching"
    return {
        "assignment": assignment,
        "load_count": row.load_count if row else 0,
        "search_load_count": row.search_load_count if row else 0,
        "last_loaded_at": last_loaded_at.isoformat() if last_loaded_at else None,
        "suggestion": suggestion,
    }
