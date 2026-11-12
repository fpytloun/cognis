"""Current authorized display labels, independent of accounting visibility."""

from __future__ import annotations

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from cognis.core.project_access import _MANAGED_CONTEXT_TYPES, PROJECT_GRANT_ROLES
from cognis.core.usage.access import AuthorizedUsageScope
from cognis.store.models import (
    Agent,
    Conversation,
    ProjectGrantRow,
    ProjectRow,
    Schedule,
    Session,
    Task,
)


async def group_labels(
    session: AsyncSession, scope: AuthorizedUsageScope, dimension: str, keys: list[str | None]
) -> dict[str | None, str | None]:
    """Batch current metadata without granting an accounting admin content access."""
    if not any(keys):
        return dict.fromkeys(keys)
    if dimension == "session":
        parent = aliased(Session)
        session_rows = (
            await session.execute(
                select(
                    Session.session_id,
                    Session.conversation_id,
                    Session.agent_id,
                    Session.parent_session_id,
                    parent.conversation_id.label("parent_conversation_id"),
                )
                .outerjoin(parent, parent.session_id == Session.parent_session_id)
                .where(Session.session_id.in_([key for key in keys if key]))
            )
        ).all()
        conversations = await group_labels(
            session,
            scope,
            "conversation",
            list({row.conversation_id for row in session_rows}),
        )
        agents = await group_labels(
            session, scope, "agent", list({row.agent_id for row in session_rows})
        )
        session_labels: dict[str | None, str | None] = dict.fromkeys(keys)
        for row in session_rows:
            title = conversations.get(row.conversation_id)
            agent = agents.get(row.agent_id)
            shares_parent = row.parent_session_id and (
                row.conversation_id == row.parent_conversation_id
            )
            session_labels[row.session_id] = (
                f"{title} · {agent}" if title and agent and shares_parent else title
            )
        return session_labels
    models = {
        "conversation": (Conversation.conversation_id, Conversation.title),
        "task": (Task.task_id, Task.title),
        "schedule": (Schedule.schedule_id, Schedule.name),
        "agent": (Agent.agent_id, Agent.name),
        "project": (ProjectRow.project_id, ProjectRow.name),
    }
    if dimension not in models:
        return {key: key for key in keys}
    identifier, label = models[dimension]
    readable_projects = select(ProjectRow.project_id).where(
        ProjectRow.status != "deleted",
        or_(
            ProjectRow.owner_email == scope.actor,
            exists(
                select(1).where(
                    ProjectGrantRow.project_id == ProjectRow.project_id,
                    ProjectGrantRow.grantee_type == "user",
                    ProjectGrantRow.grantee_user_email == scope.actor,
                    ProjectGrantRow.permission.in_([*PROJECT_GRANT_ROLES, "use"]),
                    ProjectGrantRow.revoked_at.is_(None),
                )
            ),
        ),
    )
    authority: ColumnElement[bool]
    if dimension == "project":
        # The same owner/grant readability that labels project-visible conversations.
        authority = ProjectRow.project_id.in_(readable_projects)
    elif dimension in {"schedule", "agent"}:
        # Schedule REST visibility is owner-only. Agent labels conservatively use ownership too.
        authority = (
            Schedule.created_by == scope.actor
            if dimension == "schedule"
            else Agent.owner_email == scope.actor
        )
    else:
        owner = Conversation.user_email if dimension == "conversation" else Task.created_by
        visibility = Conversation.visibility if dimension == "conversation" else Task.visibility
        project_id = Conversation.project_id if dimension == "conversation" else Task.project_id
        status = Conversation.status if dimension == "conversation" else Task.status
        authority = or_(
            owner == scope.actor,
            (visibility == "project") & project_id.in_(readable_projects),
        )
        authority = authority & (status != "deleted")
        if dimension == "conversation":
            authority = authority & or_(
                Conversation.user_email == scope.actor,
                Conversation.context_type.not_in(_MANAGED_CONTEXT_TYPES)
                & Conversation.context_data["app_contact_binding_id"].as_string().is_(None),
            )
    rows = await session.execute(
        select(identifier, label).where(identifier.in_([key for key in keys if key]), authority)
    )
    result: dict[str | None, str | None] = dict.fromkeys(keys)
    result.update((key, value) for key, value in rows.all())
    return result
