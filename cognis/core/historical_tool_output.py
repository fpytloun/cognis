"""Owner-scoped historical tool event resolution.

References locate persisted events. They never authorize output-store access.
"""

from __future__ import annotations

from typing import Any

from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import get_conversation, get_session_row


async def source_session_authority(
    registry: Any, row: Any, *, user_email: str
) -> EventStoreAuthority:
    """Resolve the same source-session identity used by Chat v2 readers."""
    if row.user_email != user_email:
        raise ValueError("Session event-store authority does not match the authorized user")
    agent = await registry.get(row.agent_id, owner_email=user_email, include_disabled=True)
    if agent is None or not agent.owner_email:
        raise ValueError("Session agent authority is unavailable")
    return EventStoreAuthority(
        user_email=user_email, agent_id=row.agent_id, agent_owner_email=agent.owner_email
    )


def tool_event_storage_id(data: dict[str, Any], call_id: str) -> str | None:
    """Bind a requested handle to a persisted tool event before store access."""
    event_call_id = data.get("call_id")
    recovery_call_id = data.get("recovery_call_id")
    if call_id != event_call_id and call_id != recovery_call_id:
        return None
    if isinstance(recovery_call_id, str) and recovery_call_id:
        return recovery_call_id
    return event_call_id if isinstance(event_call_id, str) and event_call_id else None


async def resolve_historical_tool_event(
    reference: Any,
    *,
    call_id: str,
    user_email: str,
    session_factory: Any,
    registry: Any,
    intaris: Any,
) -> dict[str, Any]:
    """Read one exact event, after ownership and membership verification."""
    if not isinstance(reference, dict) or set(reference) != {
        "conversation_id",
        "session_id",
        "seq",
        "kind",
        "call_id",
    }:
        raise ValueError("Invalid historical reference")
    if any(
        not isinstance(reference[key], str) or not reference[key] or len(reference[key]) > 512
        for key in ("conversation_id", "session_id", "call_id")
    ):
        raise ValueError("Invalid historical reference")
    seq = reference["seq"]
    if (
        isinstance(seq, bool)
        or not isinstance(seq, int)
        or seq < 1
        or reference["kind"] not in ("tool_call", "tool_result")
        or reference["call_id"] != call_id
    ):
        raise ValueError("Invalid historical reference")
    async with session_factory() as db:
        conversation = await get_conversation(db, reference["conversation_id"])
        if (
            conversation is None
            or conversation.user_email != user_email
            or conversation.status == "deleted"
        ):
            raise ValueError("Historical event not found or unavailable")
        row = await get_session_row(db, reference["session_id"])
        if (
            row is None
            or row.user_email != user_email
            or row.conversation_id != reference["conversation_id"]
        ):
            raise ValueError("Historical event not found or unavailable")
    authority = await source_session_authority(registry, row, user_email=user_email)
    with scoped_runtime_context(
        user_email=authority.user_email,
        agent_id=authority.agent_id,
        agent_owner_email=authority.agent_owner_email,
    ):
        result = await intaris.read_events(
            row.intaris_session_id or row.session_id,
            seqs=[seq],
            limit=1,
            allow_missing_stream=True,
        )
    for event in result.events:
        data = event.get("data")
        if (
            event.get("seq") == seq
            and event.get("type") == reference["kind"]
            and isinstance(data, dict)
            and data.get("call_id") == call_id
            and tool_event_storage_id(data, call_id) is not None
        ):
            return data
    raise ValueError("Historical event not found or unavailable")
