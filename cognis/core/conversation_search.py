"""Helpers for joining Intaris search results with Cognis metadata."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.models.search import (
    MIN_DISPLAY_SCORE,
    ConversationFlatSearchMatch,
    ConversationSearchMatch,
    SearchMatch,
    SearchSessionMatch,
    kind_rank,
)
from cognis.store.models import Conversation, Session
from cognis.store.queries import get_conversation, list_sessions_by_intaris_session_ids

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _intaris_id(row: Session) -> str:
    return row.intaris_session_id or row.session_id


def _index_sessions(rows: Iterable[Session]) -> dict[str, Session]:
    index: dict[str, Session] = {}
    for row in rows:
        index[row.session_id] = row
        if row.intaris_session_id:
            index[row.intaris_session_id] = row
    return index


def _same_search_match(left: SearchMatch, right: SearchMatch) -> bool:
    if left.kind != right.kind:
        return False
    if left.ref_id is not None or right.ref_id is not None:
        return left.ref_id == right.ref_id
    return left.ts == right.ts and left.snippet == right.snippet


def _normalized_query(query: str | None) -> str:
    if query is None:
        return ""
    normalized = query.strip().strip("\"'").strip()
    return normalized.casefold()


def _plain_snippet(snippet: str) -> str:
    return html.unescape(_HTML_TAG_RE.sub("", snippet)).casefold()


def _match_contains_exact_query(match: SearchMatch, query: str | None) -> bool:
    normalized = _normalized_query(query)
    if not normalized:
        return False
    snippet = match.snippet.casefold()
    if "<mark" in snippet:
        return True
    return normalized in _plain_snippet(match.snippet)


def _match_is_displayable(match: SearchMatch, *, query: str | None, min_score: float) -> bool:
    return match.score >= min_score or _match_contains_exact_query(match, query)


async def join_session_matches(
    db: AsyncSession,
    *,
    user_email: str,
    matches: list[SearchSessionMatch],
    project_id: str | None = None,
    status: str = "active",
    context_type: str | None = None,
    min_score: float = MIN_DISPLAY_SCORE,
    query: str | None = None,
) -> list[ConversationSearchMatch]:
    """Join Intaris aggregated session matches with owned Cognis conversations.

    Drops weak non-exact scores, soft-deleted conversations, and conversations
    whose Cognis-owned metadata does not match the caller's filters
    (``project_id``, ``status``, ``context_type``). Cognis is authoritative on
    those fields; Intaris does not know them.
    """

    session_rows = await list_sessions_by_intaris_session_ids(db, [m.session_id for m in matches])
    session_by_id = _index_sessions(session_rows)
    conversation_by_id: dict[str, Conversation] = {}
    output: list[ConversationSearchMatch] = []

    for match in matches:
        if not _match_is_displayable(match.top_match, query=query, min_score=min_score):
            continue
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
        starred_at = getattr(conversation, "starred_at", None)
        if status == "starred" and (starred_at is None or conversation.status != "active"):
            continue
        if status not in {"active", "starred", "archived", "all"}:
            continue
        if context_type is not None and getattr(conversation, "context_type", None) != context_type:
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


def attach_extra_matches(
    aggregated: list[ConversationSearchMatch],
    flat_matches: list[SearchMatch],
    *,
    per_session_limit: int = 4,
    min_score: float = MIN_DISPLAY_SCORE,
    query: str | None = None,
) -> None:
    """Attach supplemental hits from a flat search to aggregated session rows.

    Each session row already carries its highest-ranked ``top_match``. The
    follow-up flat search returns more granular matches across the same
    Intaris session ids; everything except the row's own top match is folded
    into ``extra_matches`` until the per-session cap is hit. Mutates
    ``aggregated`` in-place; safe when ``flat_matches`` is empty.
    """

    if not aggregated or not flat_matches:
        return
    by_session: dict[str, ConversationSearchMatch] = {
        row.intaris_session_id: row for row in aggregated
    }
    for match in flat_matches:
        if not _match_is_displayable(match, query=query, min_score=min_score):
            continue
        target = by_session.get(match.session_id)
        if target is None:
            continue
        if target.match_count <= 1:
            continue
        if _same_search_match(match, target.top_match):
            continue
        if any(_same_search_match(existing, match) for existing in target.extra_matches):
            continue
        if len(target.extra_matches) >= per_session_limit:
            continue
        target.extra_matches.append(match)
    for row in aggregated:
        row.extra_matches.sort(key=lambda m: (kind_rank(m.kind), -m.score))


async def join_flat_matches(
    db: AsyncSession,
    *,
    user_email: str,
    conversation_id: str,
    matches: list[SearchMatch],
    min_score: float = MIN_DISPLAY_SCORE,
    query: str | None = None,
) -> list[ConversationFlatSearchMatch]:
    """Join flat Intaris matches for one conversation with Cognis session rows."""

    conversation = await get_conversation(db, conversation_id)
    if conversation is None or conversation.user_email != user_email:
        return []
    if conversation.status == "deleted":
        return []
    session_rows = await list_sessions_by_intaris_session_ids(db, [m.session_id for m in matches])
    session_by_id = _index_sessions(session_rows)
    output: list[ConversationFlatSearchMatch] = []
    for match in matches:
        if not _match_is_displayable(match, query=query, min_score=min_score):
            continue
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
