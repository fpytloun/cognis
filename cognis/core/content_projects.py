"""Project scope of artifacts and deliverables.

Single owner of how a document gets its project and of which projects an
acting agent may read or write documents in. A document's project is stored on
the document itself (shared by every revision of a lineage), so access does not
depend on the chat or task that happened to produce a revision.

Project-wide access requires agent assignment and current human permission.
Human membership alone never grants access to an unrelated agent. The content
read policy separately retains directly owned documents and specific descendant
outputs; descendant reads still respect the acting work's project context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.project_access import (
    agent_usable_in_project,
    resolve_effective_project_id,
    resolve_project_role_by_id,
    role_satisfies,
)
from cognis.models.deliverable import DeliverableRevisionError
from cognis.store.models import (
    Agent,
    ArtifactRecordRow,
    Conversation,
    DeliverableRow,
    ProjectAgentRow,
    ProjectRow,
    Session,
    StepRun,
    Task,
)

ContentAccess = Literal["read", "write"]
_ROLE_FOR: dict[str, str] = {"read": "view", "write": "use"}
_MAX_SESSION_DEPTH = 16


@dataclass(frozen=True, slots=True)
class WorkProject:
    """Project of the work context that is acting, and whether that work is shared."""

    project_id: str | None
    shared: bool


async def _task_for_session(
    session: AsyncSession, session_row: Session, user_email: str
) -> Task | None:
    current: Session | None = session_row
    for _ in range(_MAX_SESSION_DEPTH):
        if current is None or current.user_email != user_email:
            return None
        task = await session.scalar(
            sa.select(Task)
            .join(StepRun, StepRun.task_id == Task.task_id)
            .where(StepRun.session_id == current.session_id, Task.created_by == user_email)
            .limit(1)
        )
        if task is not None:
            return task
        if not current.parent_session_id:
            return None
        current = await session.get(Session, current.parent_session_id)
    return None


async def acting_agent_id(
    session: AsyncSession, *, user_email: str, agent_id: str | None, session_id: str | None
) -> str | None:
    """Return the primary agent acting for a session; specialists act for their parent."""

    if not agent_id:
        return None
    if not agent_id.startswith("system:"):
        return agent_id
    current = await session.get(Session, session_id) if session_id else None
    if current is None or current.user_email != user_email or current.agent_id != agent_id:
        return None
    from cognis.core.artifact_entitlements import effective_kb_agent

    return await session.run_sync(effective_kb_agent, current)


async def work_project(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str | None,
    conversation_id: str | None,
    session_id: str | None,
) -> WorkProject:
    """Resolve the project of the acting work: a task first, then its conversation."""

    current = await session.get(Session, session_id) if session_id else None
    if current is not None and current.user_email == user_email:
        task = await _task_for_session(session, current, user_email)
        if task is not None:
            return WorkProject(
                project_id=task.project_id,
                shared=bool(task.project_id) and task.visibility == "project",
            )
        conversation_id = conversation_id or current.conversation_id
    conversation = await session.get(Conversation, conversation_id) if conversation_id else None
    if conversation is None or conversation.user_email != user_email:
        return WorkProject(project_id=None, shared=False)
    acting = await acting_agent_id(
        session, user_email=user_email, agent_id=agent_id, session_id=session_id
    )
    project_id = await resolve_effective_project_id(
        session, conversation=conversation, agent_id=acting, user_email=user_email
    )
    if not project_id:
        return WorkProject(project_id=None, shared=False)
    # A stored project is shared only when the chat is project-visible; the
    # agent-default fallback classifies the whole chat as that project's work.
    shared = conversation.project_id != project_id or conversation.visibility == "project"
    return WorkProject(project_id=project_id, shared=shared)


async def content_project_ids(
    session: AsyncSession,
    *,
    user_email: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    session_id: str | None,
    access: ContentAccess = "read",
) -> list[str]:
    """Return projects whose documents this agent session may read or write."""

    if not user_email or not agent_id:
        return []
    acting = await acting_agent_id(
        session, user_email=user_email, agent_id=agent_id, session_id=session_id
    )
    if not acting:
        return []
    if conversation_id:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.user_email != user_email:
            return []
    agent = await session.get(Agent, acting)
    if agent is None or getattr(agent, "status", "active") != "active":
        return []
    context = await work_project(
        session,
        user_email=user_email,
        agent_id=agent_id,
        conversation_id=conversation_id,
        session_id=session_id,
    )
    stmt = (
        sa.select(ProjectAgentRow.project_id)
        .join(ProjectRow, ProjectRow.project_id == ProjectAgentRow.project_id)
        .where(ProjectAgentRow.agent_id == acting, ProjectRow.status == "active")
    )
    if context.project_id:
        stmt = stmt.where(ProjectAgentRow.project_id == context.project_id)
    allowed: list[str] = []
    for project_id in (await session.scalars(stmt)).all():
        _project, role = await resolve_project_role_by_id(session, project_id, user_email)
        if not role_satisfies(role.role, _ROLE_FOR[access]):
            continue
        if await agent_usable_in_project(
            session, agent=agent, user_email=user_email, project_id=project_id
        ):
            allowed.append(str(project_id))
    return allowed


async def human_may_access_project_content(
    session: AsyncSession, *, project_id: str | None, user_email: str, access: ContentAccess
) -> bool:
    """Return whether a person (no agent involved) may read or write a project's documents."""

    if not project_id:
        return False
    _project, role = await resolve_project_role_by_id(session, project_id, user_email)
    return role_satisfies(role.role, _ROLE_FOR[access])


class ContentProjectError(DeliverableRevisionError):
    """The requested project is not writable from this agent session."""

    def __init__(self, message: str) -> None:
        super().__init__("project_not_writable", message, path="$.project_id")


async def resolve_new_content_project_id(
    session: AsyncSession,
    *,
    user_email: str,
    agent_id: str | None,
    conversation_id: str | None,
    session_id: str | None,
    requested_project_id: str | None,
    inherited_project_id: str | None = None,
    inherit: bool = False,
) -> str | None:
    """Decide the project of a new document revision.

    A revision keeps its lineage's project (``inherit``). Otherwise an explicit
    request must name a writable project; without one, the document joins the
    acting work's project when that work is shared with the project.
    """

    if inherit:
        if requested_project_id and requested_project_id != inherited_project_id:
            raise ContentProjectError("A revision keeps the project of the document it supersedes.")
        return inherited_project_id
    if requested_project_id:
        writable = await content_project_ids(
            session,
            user_email=user_email,
            agent_id=agent_id,
            conversation_id=conversation_id,
            session_id=session_id,
            access="write",
        )
        if requested_project_id not in writable:
            raise ContentProjectError(
                "The project was not found or is not writable for this agent in this context."
            )
        return requested_project_id
    context = await work_project(
        session,
        user_email=user_email,
        agent_id=agent_id,
        conversation_id=conversation_id,
        session_id=session_id,
    )
    if not context.project_id or not context.shared:
        return None
    if not await human_may_access_project_content(
        session, project_id=context.project_id, user_email=user_email, access="write"
    ):
        return None
    return context.project_id


# Documents created in a chat belong to that chat's shared project. When the
# chat's project or visibility changes, they follow it. Only lineages that
# started in the chat and still carry its previous scope move, so a document
# placed explicitly elsewhere, or started in another chat or a task, stays put.
_FOLLOWING_ARTIFACT_PURPOSES = ("artifact_write", "artifact_publish")


async def conversation_document_scope(session: AsyncSession, row: Conversation) -> str | None:
    """Return the project that new documents written in this chat would join."""

    context = await work_project(
        session,
        user_email=str(row.user_email),
        agent_id=row.agent_id,
        conversation_id=row.conversation_id,
        session_id=None,
    )
    return context.project_id if context.shared else None


def _scope_matches(
    column: sa.SQLColumnExpression[str | None], scope: str | None
) -> sa.ColumnElement[bool]:
    return column.is_(None) if scope is None else column == scope


async def lock_document_scope(session: AsyncSession, conversation_id: str | None) -> None:
    """Serialize a document write with a project or visibility change of its chat.

    Lock order is always conversation row, then document rows. A document write
    takes this lock and then resolves its project in the same transaction, so a
    concurrent sharing change either commits first (and the write sees the new
    scope) or waits for the write (and then moves the new document too).
    ``populate_existing`` refreshes an identity-mapped conversation row.
    """

    if not conversation_id:
        return
    await session.execute(
        sa.select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def move_conversation_documents(
    session: AsyncSession,
    conversation_id: str,
    *,
    old_scope: str | None,
    new_scope: str | None,
) -> int:
    """Move documents that follow a chat from its old to its new project scope.

    Every revision of a following lineage moves in the caller's transaction, so
    a lineage never spans two scopes. Returns the number of documents moved.
    """

    if old_scope == new_scope:
        return 0
    # The caller holds the conversation row through its update. Locking the
    # following lineages makes a concurrent revision writer (which locks its
    # target) either commit first, and be included by the fresh statements
    # below, or wait and then read the new scope from its locked target.
    roots = (
        sa.select(DeliverableRow.deliverable_id)
        .where(
            DeliverableRow.conversation_id == conversation_id,
            DeliverableRow.step_run_id.is_(None),
            DeliverableRow.revision == 1,
        )
        .scalar_subquery()
    )
    await session.execute(
        sa.select(DeliverableRow.deliverable_id)
        .where(DeliverableRow.lineage_id.in_(roots))
        .with_for_update()
    )
    artifact_roots = (
        sa.select(ArtifactRecordRow.artifact_id)
        .where(
            ArtifactRecordRow.conversation_id == conversation_id,
            ArtifactRecordRow.purpose.in_(_FOLLOWING_ARTIFACT_PURPOSES),
            ArtifactRecordRow.revision == 1,
        )
        .scalar_subquery()
    )
    await session.execute(
        sa.select(ArtifactRecordRow.artifact_id)
        .where(ArtifactRecordRow.lineage_id.in_(artifact_roots))
        .with_for_update()
    )
    deliverable_lineages = sa.select(DeliverableRow.deliverable_id).where(
        DeliverableRow.conversation_id == conversation_id,
        DeliverableRow.step_run_id.is_(None),
        DeliverableRow.revision == 1,
        _scope_matches(DeliverableRow.project_id, old_scope),
    )
    artifact_lineages = sa.select(ArtifactRecordRow.artifact_id).where(
        ArtifactRecordRow.conversation_id == conversation_id,
        ArtifactRecordRow.purpose.in_(_FOLLOWING_ARTIFACT_PURPOSES),
        ArtifactRecordRow.revision == 1,
        _scope_matches(ArtifactRecordRow.project_id, old_scope),
    )
    moved_deliverable_ids = list(
        (
            await session.scalars(
                sa.select(DeliverableRow.deliverable_id).where(
                    DeliverableRow.lineage_id.in_(deliverable_lineages),
                    _scope_matches(DeliverableRow.project_id, old_scope),
                )
            )
        ).all()
    )
    moved = 0
    if moved_deliverable_ids:
        result = await session.execute(
            sa.update(DeliverableRow)
            .where(DeliverableRow.deliverable_id.in_(moved_deliverable_ids))
            .values(project_id=new_scope)
            .execution_options(synchronize_session=False)
        )
        moved += int(cast(sa.CursorResult[Any], result).rowcount or 0)
        # Conversation deliverables are discovered through their artifact record.
        await session.execute(
            sa.update(ArtifactRecordRow)
            .where(ArtifactRecordRow.artifact_id.in_(moved_deliverable_ids))
            .values(project_id=new_scope)
            .execution_options(synchronize_session=False)
        )
    result = await session.execute(
        sa.update(ArtifactRecordRow)
        .where(
            ArtifactRecordRow.lineage_id.in_(artifact_lineages),
            _scope_matches(ArtifactRecordRow.project_id, old_scope),
        )
        .values(project_id=new_scope)
        .execution_options(synchronize_session=False)
    )
    moved += int(cast(sa.CursorResult[Any], result).rowcount or 0)
    return moved
