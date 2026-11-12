"""Caller-specific conversation display metadata, never permission to use an agent."""

from __future__ import annotations

from datetime import UTC
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.models import ConversationResponse
from cognis.core.project_access import resolve_agent_use_grant, resolve_project_role
from cognis.core.project_identity import project_color
from cognis.store.conversation_reads import list_conversation_read_markers
from cognis.store.models import Agent
from cognis.store.queries import get_project


async def enrich_conversation_identity(
    session: AsyncSession,
    items: list[ConversationResponse],
    *,
    user_email: str,
    can_mutate: bool,
) -> list[ConversationResponse]:
    """Enrich already-authorized rows; never expose full inaccessible agent configs."""
    agents = (
        {
            row.agent_id: row
            for row in (
                await session.scalars(
                    select(Agent).where(Agent.agent_id.in_({item.agent_id for item in items}))
                )
            ).all()
        }
        if items
        else {}
    )
    shared_reads = await list_conversation_read_markers(
        session,
        user_email,
        [item.conversation_id for item in items if item.is_readonly_for_caller],
    )
    for item in items:
        if item.is_readonly_for_caller:
            read_at = shared_reads.get(item.conversation_id)
            item.last_read_at = read_at
            message_at = item.last_message_at
            if message_at is not None and message_at.tzinfo is None:
                message_at = message_at.replace(tzinfo=UTC)
            item.has_unread = message_at is not None and (read_at is None or message_at > read_at)
        if item.project_id:
            project = await get_project(session, item.project_id)
            if project and (await resolve_project_role(session, project, user_email)).role:
                item.project_name = project.name
                item.project_color = project_color(project.project_id, project.metadata_json)
                icon = (project.metadata_json or {}).get("icon")
                item.project_icon = icon if isinstance(icon, str) and len(icon) <= 64 else None
                item.project_avatar_url = (
                    f"/api/v1/images/{quote(project.avatar_image_id, safe='')}"
                    if project.avatar_image_id
                    else project.avatar_url
                )
        agent = agents.get(item.agent_id)
        if agent is None:
            continue
        item.agent_display_name = agent.display_name or agent.name
        if agent.avatar_image_id:
            item.agent_avatar_url = (
                f"/api/v1/images/{quote(agent.avatar_image_id, safe='')}"
                f"?conversation_id={quote(item.conversation_id, safe='')}"
            )
        else:
            item.agent_avatar_url = agent.avatar_url
        if item.is_readonly_for_caller and can_mutate and agent.status == "active":
            item.can_continue = agent.owner_email == user_email or bool(
                await resolve_agent_use_grant(
                    session,
                    agent_id=agent.agent_id,
                    user_email=user_email,
                    project_id=item.project_id,
                )
            )
    return items
