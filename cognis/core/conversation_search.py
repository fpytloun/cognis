"""Helpers for joining Intaris search results with Cognis metadata."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.search import (
    ConversationFlatSearchMatch,
    ConversationSearchMatch,
    SearchMatch,
    SearchSessionMatch,
    kind_rank,
)
from cognis.store.models import Conversation, Session
from cognis.store.queries import get_conversation, list_sessions_by_intaris_session_ids


def _intaris_id(row: Session) -> str:
    return row.intaris_session_id or row.session_id


def _index_sessions(rows: Iterable[Session]) -> dict[str, Session]:
    index: dict[str, Session] = {}
    for row in rows:
        index[row.session_id] = row
        if row.intaris_session_id:
            index[row.intaris_session_id] = row
    return index


async def join_session_matches(
    db: AsyncSession,
    *,
    user_email: str,
    matches: list[SearchSessionMatch],
    project_id: str | None = None,
    status: str = "active",
) -> list[ConversationSearchMatch]:
    """Join Intaris aggregated session matches with owned Cognis conversations."""

    session_rows = await list_sessions_by_intaris_session_ids(db, [m.session_id for m in matches])
    session_by_id = _index_sessions(session_rows)
    conversation_by_id: dict[str, Conversation] = {}
    output: list[ConversationSearchMatch] = []

    for match in matches:
        session_row = session_by_id.get(match.session_id)
        if session_row is None:
            continue
        conversation = conversation_by_id.get(session_row.conversation_id)
        if conversation is None:
            conversation = await get_conversation(db, session_row.conversation_id)
            if conversation is None:
                continue
            conversation_by_id[conversation.conversation_id] = conversation
        if conversation.user_email != user_email:
            continue
        if conversation.status == "deleted":
            continue
        if project_id is not None and conversation.project_id != project_id:
            continue
        if status == "active" and conversation.status != "active":
            continue
        if status == "archived" and conversation.status != "archived":
            continue
        if status not in {"active", "archived", "all"}:
            continue
        output.append(
            ConversationSearchMatch(
                conversation_id=conversation.conversation_id,
                conversation_title=conversation.title,
                agent_id=conversation.agent_id,
                project_id=conversation.project_id,
                status=conversation.status,
                session_id=session_row.session_id,
                intaris_session_id=_intaris_id(session_row),
                title=match.title,
                intention=match.intention,
                last_activity_at=match.last_activity_at,
                match_count=match.match_count,
                top_match=match.top_match,
                kind_rank=kind_rank(match.top_match.kind),
            )
        )
    output.sort(key=lambda row: (row.kind_rank, -row.top_match.score, row.conversation_id))
    return output


async def join_flat_matches(
    db: AsyncSession,
    *,
    user_email: str,
    conversation_id: str,
    matches: list[SearchMatch],
) -> list[ConversationFlatSearchMatch]:
    """Join flat Intaris matches for one conversation with Cognis session rows."""

    conversation = await get_conversation(db, conversation_id)
    if conversation is None or conversation.user_email != user_email:
        return []
    session_rows = await list_sessions_by_intaris_session_ids(db, [m.session_id for m in matches])
    session_by_id = _index_sessions(session_rows)
    output: list[ConversationFlatSearchMatch] = []
    for match in matches:
        session_row = session_by_id.get(match.session_id)
        if session_row is None or session_row.conversation_id != conversation_id:
            continue
        output.append(
            ConversationFlatSearchMatch(
                conversation_id=conversation.conversation_id,
                conversation_title=conversation.title,
                agent_id=conversation.agent_id,
                project_id=conversation.project_id,
                status=conversation.status,
                session_id=session_row.session_id,
                intaris_session_id=_intaris_id(session_row),
                match=match,
                kind_rank=kind_rank(match.kind),
            )
        )
    output.sort(key=lambda row: (row.kind_rank, -row.match.score, row.session_id))
    return output
