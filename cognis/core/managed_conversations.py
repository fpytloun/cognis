"""Shared helpers for managed agent conversations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from cognis.logging import get_logger
from cognis.store import queries

logger = get_logger(__name__)


_SYSTEM_AGENT_PREFIX = "system:"


def is_allowed_managed_conversation_target(agent_id: str | None) -> bool:
    """Return whether an agent may own a managed conversation.

    Managed conversations are durable, visible main conversations owned by a
    target agent. System agents are specialist secondary agents and must be
    used via delegate() instead of owning managed conversations.
    """

    normalized = str(agent_id or "").strip()
    return bool(normalized) and not normalized.startswith(_SYSTEM_AGENT_PREFIX)


def managed_conversation_target_error(agent_id: str | None) -> str:
    """Return the user-facing rejection message for invalid managed targets."""

    normalized = str(agent_id or "").strip()
    if normalized.startswith(_SYSTEM_AGENT_PREFIX):
        return (
            "Managed conversations require a primary/user agent. Use delegate() "
            "for system specialist agents (`system:*`) available in this agent session."
        )
    return "Managed conversations require a primary/user agent."


@dataclass(frozen=True, slots=True)
class _SessionCandidate:
    session_id: str
    intaris_session_id: str


def _event_type(event: Any) -> str | None:
    raw_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    return raw_type if isinstance(raw_type, str) else None


def _event_data(event: Any) -> dict[str, Any] | None:
    raw_data = event.get("data") if isinstance(event, dict) else getattr(event, "data", None)
    return raw_data if isinstance(raw_data, dict) else None


def _last_user_message_from_events(events: list[Any]) -> str | None:
    for event in reversed(events):
        if _event_type(event) != "user_message":
            continue
        data = _event_data(event)
        if data is None:
            continue
        content = str(data.get("content") or "").strip()
        if content:
            return content
    return None


def _cached_events_for_session(session_cache: Any, session_id: str) -> list[Any]:
    getter = getattr(session_cache, "get_events_since_compaction", None)
    if not callable(getter):
        return []
    try:
        typed_events = getter(session_id, types=["user_message"])
    except TypeError:
        typed_events = getter(session_id)
    if not isinstance(typed_events, Iterable):
        return []
    return list(typed_events)


async def _candidate_sessions(
    session_factory: Callable[[], Any],
    link: Any,
) -> list[_SessionCandidate]:
    target_session_id = getattr(link, "target_session_id", None)
    target_conversation_id = getattr(link, "target_conversation_id", None)
    candidates: list[_SessionCandidate] = []
    seen: set[str] = set()

    def add(session_id: str | None, intaris_session_id: str | None = None) -> None:
        if not session_id or session_id in seen:
            return
        seen.add(session_id)
        candidates.append(
            _SessionCandidate(
                session_id=session_id,
                intaris_session_id=intaris_session_id or session_id,
            )
        )

    try:
        async with session_factory() as db_session:
            if not callable(getattr(db_session, "execute", None)):
                add(target_session_id)
                return candidates

            target_row = (
                await queries.get_session_row(db_session, target_session_id)
                if target_session_id
                else None
            )
            rows = (
                await queries.list_conversation_sessions(
                    db_session,
                    target_conversation_id,
                    root_only=True,
                    order="desc",
                    limit=50,
                )
                if target_conversation_id
                else []
            )
            if target_row is not None and target_row.session_id not in {
                row.session_id for row in rows
            }:
                rows.insert(0, target_row)
            for row in rows:
                add(row.session_id, row.intaris_session_id or row.session_id)
            if target_row is None:
                add(target_session_id)
    except Exception:
        logger.warning(
            "managed conversation: failed to list retry session candidates",
            extra={
                "extra_data": {
                    "link_id": getattr(link, "link_id", None),
                    "target_conversation_id": target_conversation_id,
                    "target_session_id": target_session_id,
                }
            },
            exc_info=True,
        )
        add(target_session_id)

    return candidates


async def last_managed_conversation_user_message_for_retry(
    *,
    session_cache: Any,
    guardrails: Any,
    session_factory: Callable[[], Any],
    link: Any,
) -> str | None:
    """Return the newest durable target user message available for retry."""

    read_events: Callable[..., Awaitable[Any]] | None = getattr(guardrails, "read_events", None)
    for candidate in await _candidate_sessions(session_factory, link):
        cached_message = _last_user_message_from_events(
            _cached_events_for_session(session_cache, candidate.session_id)
        )
        if cached_message:
            return cached_message
        if not callable(read_events):
            continue
        try:
            events: list[Any] = []
            after_seq = 0
            while True:
                result = await read_events(
                    session_id=candidate.intaris_session_id,
                    after_seq=after_seq,
                    limit=500,
                    types=["user_message"],
                    allow_missing_stream=True,
                )
                events.extend(list(getattr(result, "events", []) or []))
                last_seq = int(getattr(result, "last_seq", 0) or 0)
                if not getattr(result, "has_more", False) or last_seq <= after_seq:
                    break
                after_seq = last_seq
        except Exception:
            logger.warning(
                "managed conversation: failed to read retry user messages",
                extra={
                    "extra_data": {
                        "link_id": getattr(link, "link_id", None),
                        "session_id": candidate.session_id,
                        "intaris_session_id": candidate.intaris_session_id,
                    }
                },
                exc_info=True,
            )
            continue
        durable_message = _last_user_message_from_events(events)
        if durable_message:
            return durable_message
    return None
