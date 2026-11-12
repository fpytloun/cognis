"""Transport-neutral project access policy.

Single owner for project roles, project-scoped resource visibility, and agent
access derived from project assignments. There is no admin bypass anywhere in
this module; access comes from ownership or an explicit grant only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.long_lived_chat import is_long_lived_chat_context
from cognis.models.session import ConversationContext

ProjectRole = Literal["owner", "manager", "member", "viewer"]
ProjectRequirement = Literal["view", "use", "manage", "share"]

PROJECT_GRANT_ROLES: Final = ("viewer", "member", "manager")
_ROLE_RANK: Final[dict[str, int]] = {"viewer": 1, "member": 2, "manager": 3, "owner": 4}
_REQUIRED_RANK: Final[dict[str, int]] = {"view": 1, "use": 2, "manage": 3, "share": 4}
PROJECT_VISIBILITIES: Final = ("project", "private")
_MANAGED_CONTEXT_TYPES: Final = frozenset({"agent_work", "managed_agent_conversation"})


def normalize_project_role(permission: str | None) -> str | None:
    """Map a stored grant permission to a role.

    ``use`` is the pre-role grant value and keeps its original meaning:
    the grantee may use the project, which is the ``member`` role.
    """

    value = str(permission or "").strip().lower()
    if value == "use":
        return "member"
    return value if value in PROJECT_GRANT_ROLES else None


def role_satisfies(role: str | None, required: str) -> bool:
    """Return whether ``role`` meets a project access requirement."""

    if role is None:
        return False
    if required not in _REQUIRED_RANK:
        raise ValueError(f"Unsupported project access requirement: {required}")
    return _ROLE_RANK.get(role, 0) >= _REQUIRED_RANK[required]


@dataclass(slots=True)
class ProjectRoleResolution:
    """Caller role on one project plus the grant that conferred it."""

    role: str | None
    grant: Any | None = None


async def resolve_project_role(
    session: AsyncSession, project: Any, user_email: str
) -> ProjectRoleResolution:
    """Resolve a user's role on a project (``None`` when the user has no access)."""

    if project is None or getattr(project, "status", None) == "deleted":
        return ProjectRoleResolution(role=None)
    if str(getattr(project, "owner_email", "") or "") == user_email:
        return ProjectRoleResolution(role="owner")
    from cognis.store.queries import get_active_project_grant

    grant = await get_active_project_grant(session, str(project.project_id), user_email)
    if grant is None:
        return ProjectRoleResolution(role=None)
    return ProjectRoleResolution(role=normalize_project_role(grant.permission), grant=grant)


async def resolve_project_role_by_id(
    session: AsyncSession, project_id: str | None, user_email: str
) -> tuple[Any | None, ProjectRoleResolution]:
    """Load a project and resolve the caller role in one step."""

    if not project_id:
        return None, ProjectRoleResolution(role=None)
    from cognis.store.queries import get_project

    project = await get_project(session, project_id)
    return project, await resolve_project_role(session, project, user_email)


def append_project_instructions(existing: str | None, addition: str) -> str:
    """Append a paragraph to project instructions without rewriting the rest."""

    current = (existing or "").rstrip()
    addition = addition.strip()
    if not current:
        return addition
    if not addition:
        return current
    return f"{current}\n\n{addition}"


# --- Resource visibility ---


async def can_read_project_resource(
    session: AsyncSession,
    *,
    author_email: str,
    project_id: str | None,
    visibility: str | None,
    user_email: str,
) -> bool:
    """Return whether ``user_email`` may read a conversation or task.

    Authors always read their own resources. Others need the resource to be
    project-visible and at least the ``viewer`` role on its project.
    """

    if author_email == user_email:
        return True
    if not project_id or visibility != "project":
        return False
    _project, resolution = await resolve_project_role_by_id(session, project_id, user_email)
    return role_satisfies(resolution.role, "view")


async def can_read_conversation(session: AsyncSession, row: Any, user_email: str) -> bool:
    """Return whether a conversation row is readable by ``user_email``."""

    if row is None or getattr(row, "status", None) == "deleted":
        return False
    if (
        getattr(row, "context_type", None) in _MANAGED_CONTEXT_TYPES
        or bool((getattr(row, "context_data", None) or {}).get("app_contact_binding_id"))
    ) and row.user_email != user_email:
        return False
    return await can_read_project_resource(
        session,
        author_email=str(row.user_email),
        project_id=getattr(row, "project_id", None),
        visibility=getattr(row, "visibility", None),
        user_email=user_email,
    )


async def can_read_task(session: AsyncSession, row: Any, user_email: str) -> bool:
    """Return whether a task row is readable by ``user_email``."""

    if row is None:
        return False
    return await can_read_project_resource(
        session,
        author_email=str(row.created_by),
        project_id=getattr(row, "project_id", None),
        visibility=getattr(row, "visibility", None),
        user_email=user_email,
    )


async def _project_document_readable(session: AsyncSession, row: Any, user_email: str) -> bool:
    """A document stored in a project is readable by people with a project role."""

    project_id = getattr(row, "project_id", None)
    if not project_id:
        return False
    _project, resolution = await resolve_project_role_by_id(session, str(project_id), user_email)
    return role_satisfies(resolution.role, "view")


async def can_project_read_deliverable(session: AsyncSession, row: Any, user_email: str) -> bool:
    """Return whether a non-author project reader may read a deliverable.

    The deliverable inherits readability from its conversation, or from its
    task when it was produced by a workflow step.
    """

    if row is None:
        return False
    if await _project_document_readable(session, row, user_email):
        return True
    step_run_id = getattr(row, "step_run_id", None)
    if step_run_id:
        from cognis.core.content_refs import task_for_step_run

        task = await task_for_step_run(session, str(step_run_id))
        # Authors are served by owner paths (with owner 404 semantics).
        if task is None or str(task.created_by) == user_email:
            return False
        return await can_read_task(session, task, user_email)
    conversation_id = getattr(row, "conversation_id", None)
    if not conversation_id:
        return False
    from cognis.store.queries import get_conversation

    conversation = await get_conversation(session, str(conversation_id))
    if conversation is None or str(conversation.user_email) == user_email:
        return False
    return await can_read_conversation(session, conversation, user_email)


async def can_project_read_artifact(session: AsyncSession, row: Any, user_email: str) -> bool:
    """Return whether a project reader may read an artifact.

    Readable as a project document, or attached to a readable conversation.
    """

    if row is None:
        return False
    if await _project_document_readable(session, row, user_email):
        return True
    conversation_id = getattr(row, "conversation_id", None)
    if not conversation_id:
        return False
    from cognis.store.queries import get_conversation

    conversation = await get_conversation(session, str(conversation_id))
    return conversation is not None and await can_read_conversation(
        session, conversation, user_email
    )


# --- Conversation project assignment ---


def conversation_context(row: Any) -> ConversationContext:
    """Build the context predicate input from a conversation row or model."""

    if isinstance(row, ConversationContext):
        return row
    context = getattr(row, "context", None)
    if isinstance(context, ConversationContext):
        return context
    return ConversationContext(
        type=str(getattr(row, "context_type", "") or ""),
        ref=getattr(row, "context_ref", None),
        platform_data=dict(getattr(row, "context_data", None) or {}),
    )


def project_assignment_refusal(row: Any) -> str | None:
    """Return why a conversation cannot carry an explicit project, or ``None``.

    Only topic conversations store a project. Channel, main, and agent-direct
    chats derive their project from the agent default; task control and
    managed conversations inherit from their owning task or controller.
    """

    context = conversation_context(row)
    if is_long_lived_chat_context(context):
        return (
            "Channel, main, and agent-direct chats cannot be assigned to a project; "
            "they use the agent's default project."
        )
    data = context.platform_data if isinstance(context.platform_data, dict) else {}
    if data.get("app_contact_binding_id"):
        return "Managed app channel exchanges are private to their recipient."
    if data.get("kind") == "task_control" or str(context.type) == "task":
        return "Task conversations use their task's project."
    if str(context.type) in _MANAGED_CONTEXT_TYPES:
        return "Managed conversations use their controller's project."
    return None


class ProjectAssignmentError(ValueError):
    """Rejected conversation project change, with an HTTP-compatible status."""

    def __init__(self, message: str, *, status_code: int, code: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def change_conversation_project(
    session: AsyncSession,
    row: Any,
    *,
    project_id: str | None,
    user_email: str,
    visibility: str | None = None,
) -> None:
    """Validate and apply an explicit project change on a conversation row.

    Shared by the REST API, the ``/project`` command, and the agent tool. An
    optional ``visibility`` is applied in the same change, so the chat's
    documents move once, from the original scope to the final one. The caller
    commits the session.
    """

    if str(row.user_email) != user_email:
        raise ProjectAssignmentError(
            "Only the conversation author can change its project",
            status_code=403,
            code="forbidden",
        )
    # Lock and refresh the chat first: validation and the original document
    # scope must come from committed state, not a concurrently stale row.
    await _lock_conversation(session, row)
    refusal = project_assignment_refusal(row)
    if refusal is not None:
        raise ProjectAssignmentError(refusal, status_code=409, code="project_not_assignable")
    if project_id is not None:
        project, resolution = await resolve_project_role_by_id(session, project_id, user_email)
        if project is None or resolution.role is None:
            raise ProjectAssignmentError("Project not found", status_code=404, code="not_found")
        if project.status != "active":
            raise ProjectAssignmentError(
                "Project is not active", status_code=409, code="project_not_active"
            )
        if not role_satisfies(resolution.role, "use"):
            raise ProjectAssignmentError(
                "Your project role does not allow adding conversations",
                status_code=403,
                code="forbidden",
            )
    from cognis.store.queries import get_agent

    agent = await get_agent(session, str(row.agent_id))
    if agent is not None and not await agent_usable_in_project(
        session, agent=agent, user_email=user_email, project_id=project_id
    ):
        raise ProjectAssignmentError(
            "This agent is available to you only inside its shared project",
            status_code=409,
            code="agent_not_available_in_project",
        )
    from cognis.core.content_projects import conversation_document_scope

    old_scope = await conversation_document_scope(session, row)
    if row.project_id is None and project_id is not None:
        # A chat added to a project is shared with it; keeping it private is an
        # explicit choice made afterwards.
        row.visibility = "project"
    if visibility is not None:
        row.visibility = visibility
    row.project_id = project_id
    await _move_documents_to_current_scope(session, row, old_scope)


async def change_conversation_visibility(
    session: AsyncSession,
    row: Any,
    *,
    visibility: str,
) -> None:
    """Apply a conversation visibility change; its documents follow. Caller commits."""

    from cognis.core.content_projects import conversation_document_scope

    await _lock_conversation(session, row)
    old_scope = await conversation_document_scope(session, row)
    row.visibility = visibility
    await _move_documents_to_current_scope(session, row, old_scope)


async def _lock_conversation(session: AsyncSession, row: Any) -> None:
    """Serialize sharing changes and document writes on one chat (see lock_document_scope)."""

    from cognis.core.content_projects import lock_document_scope

    await lock_document_scope(session, str(row.conversation_id))


async def _move_documents_to_current_scope(
    session: AsyncSession, row: Any, old_scope: str | None
) -> None:
    from cognis.core.content_projects import (
        conversation_document_scope,
        move_conversation_documents,
    )

    await session.flush()
    await move_conversation_documents(
        session,
        str(row.conversation_id),
        old_scope=old_scope,
        new_scope=await conversation_document_scope(session, row),
    )


async def resolve_effective_project_id(
    session: AsyncSession,
    *,
    conversation: Any,
    agent_id: str | None,
    user_email: str,
) -> str | None:
    """Return the project a conversation's work belongs to.

    A stored project wins. Long-lived chats fall back to the agent's default
    project when the acting user may use that project.
    """

    stored = getattr(conversation, "project_id", None)
    if isinstance(stored, str) and stored.strip():
        return stored
    if conversation is None or agent_id is None:
        return None
    if not is_long_lived_chat_context(conversation_context(conversation)):
        return None
    from cognis.store.project_queries import get_default_project_for_agent

    project = await get_default_project_for_agent(session, agent_id)
    if project is None:
        return None
    resolution = await resolve_project_role(session, project, user_email)
    return str(project.project_id) if role_satisfies(resolution.role, "use") else None


async def default_project_id_for_new_topic(
    session: AsyncSession, *, agent_id: str, user_email: str
) -> str | None:
    """Return the agent default project to apply to a new topic conversation."""

    from cognis.store.project_queries import get_default_project_for_agent

    project = await get_default_project_for_agent(session, agent_id)
    if project is None:
        return None
    resolution = await resolve_project_role(session, project, user_email)
    return str(project.project_id) if role_satisfies(resolution.role, "use") else None


class ManagedProjectError(ValueError):
    """The requested project cannot hold the new managed conversation."""


async def managed_conversation_project_id(
    session: AsyncSession,
    *,
    requested_project_id: str | None,
    controller_project_id: str | None,
    target_agent: Any,
    user_email: str,
) -> str | None:
    """Return the project of a new managed conversation.

    An explicit request from the controlling agent wins, then the controlling
    conversation's project, then the target agent's default project. An
    explicit project must be active and usable by the user and the target agent.
    """

    if requested_project_id:
        project, role = await resolve_project_role_by_id(session, requested_project_id, user_email)
        if (
            project is None
            or getattr(project, "status", None) != "active"
            or not role_satisfies(role.role, "use")
            or not await agent_usable_in_project(
                session, agent=target_agent, user_email=user_email, project_id=requested_project_id
            )
        ):
            raise ManagedProjectError("The project was not found or is not usable for this agent.")
        return requested_project_id
    if controller_project_id:
        return controller_project_id
    return await default_project_id_for_new_topic(
        session, agent_id=target_agent.agent_id, user_email=user_email
    )


# --- Agent access through project assignment ---


@dataclass(slots=True)
class ProjectAgentGrant:
    """Agent grant derived from a project assignment.

    Mirrors the attributes runtime code reads from ``AgentGrantRow`` so the
    existing shared-agent paths (executor scope, memory keying) apply. It is
    valid only for work inside ``project_id``.
    """

    agent_id: str
    grantee_user_email: str
    project_id: str
    executor_scope: str
    granted_by: str
    granted_at: datetime | None
    permission: str = "use"
    grantee_type: str = "user"
    grantee_overrides: dict[str, Any] | None = None
    revoked_at: datetime | None = None
    note: str | None = None
    grant_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.grant_id:
            self.grant_id = f"project:{self.project_id}:{self.agent_id}"


async def resolve_agent_use_grant(
    session: AsyncSession,
    *,
    agent_id: str,
    user_email: str,
    project_id: str | None,
) -> Any | None:
    """Return the grant letting a non-owner use an agent, direct grants first.

    A project-derived grant is returned only when ``project_id`` names an
    active project the agent is assigned to and the user may use it.
    """

    from cognis.store.queries import get_active_agent_grant

    direct = await get_active_agent_grant(session, agent_id, user_email)
    if direct is not None:
        return direct
    if not project_id:
        return None
    from cognis.store.project_queries import get_project_agent_use_assignment

    assignment = await get_project_agent_use_assignment(
        session, agent_id=agent_id, user_email=user_email, project_id=project_id
    )
    if assignment is None:
        return None
    return ProjectAgentGrant(
        agent_id=agent_id,
        grantee_user_email=user_email,
        project_id=project_id,
        executor_scope=str(assignment.executor_scope or "owner_executor"),
        granted_by=str(assignment.added_by),
        granted_at=getattr(assignment, "created_at", None),
    )


async def resolve_any_project_agent_view_grant(
    session: AsyncSession, *, agent_id: str, user_email: str
) -> ProjectAgentGrant | None:
    """Return a view-only marker grant when any project shares the agent with the user."""

    from cognis.store.project_queries import list_project_shared_agents_for_user

    for agent, assignment, project in await list_project_shared_agents_for_user(
        session, user_email
    ):
        if agent.agent_id == agent_id:
            return ProjectAgentGrant(
                agent_id=agent_id,
                grantee_user_email=user_email,
                project_id=str(project.project_id),
                executor_scope=str(assignment.executor_scope or "owner_executor"),
                granted_by=str(assignment.added_by),
                granted_at=getattr(assignment, "created_at", None),
            )
    return None


async def agent_usable_in_project(
    session: AsyncSession,
    *,
    agent: Any,
    user_email: str,
    project_id: str | None,
) -> bool:
    """Return whether ``user_email`` may run ``agent`` for work in ``project_id``."""

    if bool(getattr(agent, "is_system", False)):
        return True
    if str(getattr(agent, "owner_email", "") or "") == user_email:
        return True
    grant = await resolve_agent_use_grant(
        session, agent_id=str(agent.agent_id), user_email=user_email, project_id=project_id
    )
    return grant is not None
