"""Durable active-conversation discovery for client-independent snapshot warming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import exists, or_, select

from cognis.store.direct_turns import NONTERMINAL_STATUSES
from cognis.store.models import DirectTurnRequestRow, ManagedConversationLink, Session, StepRun

DIRECT_ACTIVE_STATES = tuple(status.value for status in NONTERMINAL_STATUSES)
LINKED_ACTIVE_STATES = ("pending", "queued", "running", "waiting", "paused")


async def resolve_event_session_conversation_id(session: Any, session_id: str) -> str | None:
    value = await session.scalar(
        select(Session.conversation_id)
        .where(
            or_(
                Session.intaris_session_id == session_id,
                Session.session_id == session_id,
            )
        )
        .limit(1)
    )
    return str(value) if value else None


async def conversation_needs_snapshot_warm(
    session_factory: Any,
    snapshot_cache: Any,
    conversation_id: str,
) -> bool:
    """Recognize direct/channel and generic linked task/workflow/managed work."""

    if snapshot_cache.has_warm_scope(f"conversation:{conversation_id}"):
        return True
    async with session_factory() as session:
        return bool(
            await session.scalar(
                select(
                    or_(
                        exists().where(
                            DirectTurnRequestRow.conversation_id == conversation_id,
                            DirectTurnRequestRow.status.in_(DIRECT_ACTIVE_STATES),
                        ),
                        exists().where(
                            ManagedConversationLink.target_conversation_id == conversation_id,
                            ManagedConversationLink.conversation_state == "open",
                            or_(
                                ManagedConversationLink.turn_state.in_(("queued", "running")),
                                ManagedConversationLink.active_turn_id.is_not(None),
                            ),
                        ),
                        exists().where(
                            StepRun.conversation_id == conversation_id,
                            StepRun.status.in_(LINKED_ACTIVE_STATES),
                        ),
                    )
                )
            )
        )


async def iter_active_snapshot_conversation_ids(
    session_factory: Any,
    *,
    page_size: int = 128,
) -> AsyncIterator[str]:
    """Keyset-page every durable active source without a startup stampede."""

    queries = (
        (
            DirectTurnRequestRow.conversation_id,
            (DirectTurnRequestRow.status.in_(DIRECT_ACTIVE_STATES),),
        ),
        (
            ManagedConversationLink.target_conversation_id,
            (
                ManagedConversationLink.conversation_state == "open",
                or_(
                    ManagedConversationLink.turn_state.in_(("queued", "running")),
                    ManagedConversationLink.active_turn_id.is_not(None),
                ),
            ),
        ),
        (
            StepRun.conversation_id,
            (
                StepRun.conversation_id.is_not(None),
                StepRun.status.in_(LINKED_ACTIVE_STATES),
            ),
        ),
    )
    for conversation_column, conditions in queries:
        cursor: str | None = None
        while True:
            statement = (
                select(conversation_column)
                .distinct()
                .where(*conditions)
                .order_by(conversation_column)
                .limit(page_size)
            )
            if cursor is not None:
                statement = statement.where(conversation_column > cursor)
            async with session_factory() as session:
                page = [str(item) for item in (await session.execute(statement)).scalars() if item]
            for conversation_id in page:
                yield conversation_id
            if len(page) < page_size:
                break
            cursor = page[-1]
