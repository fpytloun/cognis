"""Personal read markers for project-shared conversations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.models import ConversationReadState


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def list_conversation_read_markers(
    session: AsyncSession, user_email: str, conversation_ids: Sequence[str]
) -> dict[str, datetime]:
    """Read only the caller's markers for an already authorized bounded page."""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(ConversationReadState.conversation_id, ConversationReadState.last_read_at).where(
            ConversationReadState.user_email == user_email,
            ConversationReadState.conversation_id.in_(conversation_ids),
        )
    )
    return {conversation_id: _utc(read_at) for conversation_id, read_at in rows}


async def mark_shared_conversation_read(
    session: AsyncSession,
    conversation_id: str,
    user_email: str,
    *,
    read_at: datetime | None = None,
) -> datetime:
    """Advance one reader's marker atomically; the caller owns authorization/commit."""
    dialect = session.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise ValueError(f"Unsupported read-marker database dialect: {dialect}")
    statement: Any = (
        postgresql_insert(ConversationReadState)
        if dialect == "postgresql"
        else sqlite_insert(ConversationReadState)
    ).values(
        user_email=user_email,
        conversation_id=conversation_id,
        last_read_at=_utc(read_at or datetime.now(UTC)),
    )
    statement = statement.on_conflict_do_update(
        index_elements=["user_email", "conversation_id"],
        set_={
            "last_read_at": case(
                (
                    statement.excluded.last_read_at > ConversationReadState.last_read_at,
                    statement.excluded.last_read_at,
                ),
                else_=ConversationReadState.last_read_at,
            )
        },
    ).returning(ConversationReadState.last_read_at)
    result = await session.execute(statement)
    return _utc(result.scalar_one())
