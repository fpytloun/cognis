"""Read/use visibility for skills assigned to a currently shared agent."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.content_projects import work_project
from cognis.core.project_access import resolve_agent_use_grant
from cognis.runtime_context import current_runtime_access_context
from cognis.store.models import SkillRow
from cognis.store.queries import get_agent


async def list_readable_skills(
    session: AsyncSession,
    *,
    actor_email: str,
    agent_id: str | None = None,
    skill_id: str | None = None,
) -> list[SkillRow]:
    """Add enabled, assigned owner skills only under a fresh agent-use grant.

    This policy grants no mutation or general artifact entitlement.
    """
    from cognis.tools.skills import extract_agent_skill_refs, is_agent_assignable_skill

    context = current_runtime_access_context.get()
    if context is not None and context.user_email != actor_email:
        context = None
    if agent_id is None and context is not None:
        agent_id = context.agent_id
    visible = or_(SkillRow.owner_email == actor_email, SkillRow.owner_email.is_(None))
    agent = await get_agent(session, agent_id) if agent_id else None
    if (
        agent is not None
        and agent.status == "active"
        and agent.owner_email != actor_email
        and (
            context is None
            or context.agent_id != agent_id
            or not context.agent_owner_email
            or context.agent_owner_email == agent.owner_email
        )
    ):
        scope = await work_project(
            session,
            user_email=actor_email,
            agent_id=agent.agent_id,
            conversation_id=context.conversation_id if context else None,
            session_id=context.session_id if context else None,
        )
        grant = await resolve_agent_use_grant(
            session,
            agent_id=agent.agent_id,
            user_email=actor_email,
            project_id=scope.project_id,
        )
        if grant is not None:
            assigned = [
                ref.skill_id
                for ref in extract_agent_skill_refs(agent)
                if ref.enabled and is_agent_assignable_skill(ref.skill_id)
            ]
            visible = or_(
                visible,
                (SkillRow.owner_email == agent.owner_email) & SkillRow.skill_id.in_(assigned),
            )
    stmt = select(SkillRow).where(visible).order_by(SkillRow.name)
    if skill_id is not None:
        stmt = stmt.where(SkillRow.skill_id == skill_id)
    return list((await session.scalars(stmt)).all())
