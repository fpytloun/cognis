"""Authorized event-store readers for Chat v2 session rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cognis.api.chat_v2.sync import ConversationSessionRef
from cognis.api.common import api_exception
from cognis.core.historical_tool_output import source_session_authority
from cognis.providers.guardrails.events import EventStoreAuthority
from cognis.store.models import Agent


async def session_read_ref(
    app: Any,
    session_row: Any,
    *,
    user_email: str,
    role: str,
    ordinal: int,
) -> ConversationSessionRef:
    try:
        authority = await source_session_authority(
            app.state.agent_registry, session_row, user_email=user_email
        )
    except ValueError as exc:
        raise api_exception(500, "event_store_authority_unavailable", str(exc)) from exc
    return _bind_ref(
        app,
        session_row,
        user_email=user_email,
        agent_owner_email=authority.agent_owner_email,
        role=role,
        ordinal=ordinal,
    )


async def session_read_refs(
    app: Any,
    session_rows: list[Any],
    *,
    user_email: str,
    role: str,
    start_ordinal: int = 0,
) -> list[ConversationSessionRef]:
    """Bind session rows with one bounded authority lookup."""

    if not session_rows:
        return []
    for session_row in session_rows:
        if session_row.user_email != user_email:
            raise api_exception(
                500,
                "event_store_authority_unavailable",
                "Session event-store authority does not match the authorized user",
            )

    agent_ids = {str(row.agent_id) for row in session_rows}
    owners: dict[str, str] = {}
    registry = app.state.agent_registry
    get_system_agent = getattr(registry, "get_system_agent", None)
    for agent_id in agent_ids:
        system_agent = get_system_agent(agent_id) if callable(get_system_agent) else None
        if system_agent is not None:
            owners[agent_id] = system_agent.owner_email

    database_agent_ids = agent_ids - owners.keys()
    if database_agent_ids:
        async with app.state.session_factory() as session:
            rows = (
                await session.execute(
                    select(Agent.agent_id, Agent.owner_email).where(
                        Agent.agent_id.in_(database_agent_ids)
                    )
                )
            ).all()
        owners.update({str(agent_id): str(owner_email) for agent_id, owner_email in rows})

    if len(owners) != len(agent_ids):
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Session agent authority is unavailable",
        )
    return [
        _bind_ref(
            app,
            session_row,
            user_email=user_email,
            agent_owner_email=owners[str(session_row.agent_id)],
            role=role,
            ordinal=start_ordinal + offset,
        )
        for offset, session_row in enumerate(session_rows)
    ]


def _bind_ref(
    app: Any,
    session_row: Any,
    *,
    user_email: str,
    agent_owner_email: str,
    role: str,
    ordinal: int,
) -> ConversationSessionRef:
    cached_store = getattr(app.state, "cached_event_store", None)
    if cached_store is None or not callable(getattr(cached_store, "bind", None)):
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Cached session event store is not configured",
        )
    reader = cached_store.bind(
        EventStoreAuthority(
            user_email=user_email,
            agent_id=session_row.agent_id,
            agent_owner_email=agent_owner_email,
        )
    )
    authority_token = getattr(reader, "authority_token", None)
    if not isinstance(authority_token, str) or not authority_token:
        raise api_exception(
            500,
            "event_store_authority_unavailable",
            "Cached session event-store authority token is unavailable",
        )
    return ConversationSessionRef(
        session_id=session_row.session_id,
        event_store_session_id=session_row.intaris_session_id or session_row.session_id,
        store="intaris",
        role=role,
        ordinal=ordinal,
        status=session_row.status,
        completion_reason=session_row.completion_reason,
        reader=reader,
        authority_token=authority_token,
    )


__all__ = ["session_read_ref", "session_read_refs"]
