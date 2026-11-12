"""Project CRUD routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import IntegrityError

from cognis.api.common import (
    api_exception,
    apply_project_access_metadata,
    check_project_access,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.conversation_summaries import conversation_list_summaries
from cognis.api.models import (
    ConversationResponse,
    CursorPage,
    ProjectActivityResponse,
    ProjectAgentResponse,
    ProjectAgentUpsertRequest,
    ProjectArtifactItem,
    ProjectAvatarGenerateResponse,
    ProjectCreateRequest,
    ProjectDeliverableItem,
    ProjectGrantCreateRequest,
    ProjectGrantResponse,
    ProjectGrantUpdateRequest,
    ProjectLeaveRequest,
    ProjectResponse,
    ProjectSourceCreateRequest,
    ProjectSourceResponse,
    ProjectSourceUpdateRequest,
    ProjectTaskSummaryResponse,
    ProjectUpdateRequest,
)
from cognis.api.project_pagination import decode_project_cursor, encode_project_cursor
from cognis.api.serializers import (
    project_agent_to_response,
    project_grant_to_response,
    project_source_to_response,
    project_to_response,
)
from cognis.core.project_access import append_project_instructions
from cognis.core.project_knowledge import project_knowledgebase_metadata, set_project_knowledgebase
from cognis.core.project_management import ProjectManagementError
from cognis.models.knowledgebase import KnowledgebaseModel
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.project_knowledge import list_project_knowledgebases
from cognis.store.project_queries import (
    get_project_agent,
    latest_visible_project_activity,
    list_active_project_grants,
    list_default_agent_ids_for_project,
    list_project_agents,
    list_project_artifacts,
    list_project_conversations,
    list_project_deliverables,
    list_project_tasks,
    remove_project_agent,
    upsert_project_agent,
)
from cognis.store.queries import (
    attach_project_workflow,
    count_active_schedules_for_project,
    create_project,
    create_project_source,
    delete_project_source,
    detach_project_workflow,
    get_agent,
    get_project,
    get_project_source,
    list_project_grants,
    list_project_sources,
    list_project_workflow_ids,
    list_projects_for_user,
    update_project,
    update_project_source,
)

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


async def _require_project(request: Request, project_id: str, *, required: str = "view") -> Any:
    async with request.app.state.session_factory() as session:
        project = await get_project(session, project_id)
    if project is None or project.status == "deleted":
        raise api_exception(404, "not_found", "Project not found")
    access = await check_project_access(request, project, required=required)
    return apply_project_access_metadata(project, access)


async def _project_response(
    request: Request, project: Any, *, include_grants: bool = False
) -> ProjectResponse:
    user = require_current_user(request)
    if getattr(project, "caller_role", None) is None:
        access = await check_project_access(request, project, required="view")
        project = apply_project_access_metadata(project, access)
    async with request.app.state.session_factory() as session:
        sources = await list_project_sources(session, project.project_id)
        workflow_ids = await list_project_workflow_ids(session, project.project_id)
        active_schedule_count = await count_active_schedules_for_project(
            session,
            project.project_id,
            created_by=user.email,
        )
        grants: list[Any] = []
        if include_grants:
            # Owners see grant history; every other member sees who is in the project.
            grants = (
                await list_project_grants(session, project.project_id)
                if project.owner_email == user.email
                else await list_active_project_grants(session, project.project_id)
            )
        agents = await list_project_agents(session, project.project_id)
    return project_to_response(
        project,
        sources=sources,
        workflow_ids=workflow_ids,
        active_schedule_count=active_schedule_count,
        grants=grants,
        agents=[
            project_agent_to_response(assignment, agent, viewer_email=user.email)
            for assignment, agent in agents
        ],
    )


async def _require_project_workflow(request: Request, *, workflow_id: str, project_id: str) -> None:
    """Validate a workflow can be used by the caller's project."""

    try:
        await request.app.state.project_management_service.require_workflow(
            workflow_id, project_id, require_current_user(request).email
        )
    except ProjectManagementError as exc:
        raise api_exception(exc.status, exc.code, str(exc)) from exc


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    request: Request,
    status: str | None = Query(default="active"),
    q: str | None = Query(default=None),
    order_by_activity: bool = Query(default=False),
) -> list[ProjectResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_projects_for_user(session, user.email, status=status, query=q)
        if order_by_activity:
            from datetime import UTC, datetime

            activity: dict[str, datetime] = {}
            for row in rows:
                latest = await latest_visible_project_activity(
                    session, row.project_id, viewer_email=user.email
                )
                updated = row.updated_at or row.created_at
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                activity[row.project_id] = latest or updated
            rows.sort(key=lambda row: (activity[row.project_id], row.project_id), reverse=True)
    result: list[ProjectResponse] = []
    for row in rows:
        access = await check_project_access(request, row, required="view")
        result.append(
            await _project_response(
                request, apply_project_access_metadata(row, access), include_grants=True
            )
        )
    return result


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project_route(request: Request, payload: ProjectCreateRequest) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    if payload.default_workflow_id is not None:
        workflow = await request.app.state.workflow_registry.get(
            payload.default_workflow_id,
            owner_email=user.email,
            project_id=None,
        )
        if workflow is None or (
            not bool(getattr(workflow, "is_system", False))
            and getattr(workflow, "owner_email", None) != user.email
        ):
            raise api_exception(404, "not_found", "Workflow not found")
    avatar_image_id = payload.avatar_image_id
    if avatar_image_id:
        try:
            avatar_image_id = await request.app.state.project_management_service.avatar_snapshot(
                avatar_image_id, actor=user.email, project_owner=user.email
            )
        except ProjectManagementError as exc:
            raise api_exception(exc.status, exc.code, str(exc)) from exc
    async with request.app.state.session_factory() as session:
        row = await create_project(
            session,
            owner_email=user.email,
            name=payload.name,
            description=payload.description,
            instructions=payload.instructions,
            default_workflow_id=payload.default_workflow_id,
            avatar_image_id=avatar_image_id,
            avatar_url=payload.avatar_url,
            metadata=payload.metadata,
        )
        await session.commit()
        await session.refresh(row)
    return await _project_response(request, row)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project_route(request: Request, project_id: str) -> ProjectResponse:
    project = await _require_project(request, project_id)
    return await _project_response(request, project, include_grants=True)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project_route(
    request: Request,
    project_id: str,
    payload: ProjectUpdateRequest,
) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    project = await _require_project(request, project_id, required="manage")
    if payload.status is not None:
        await check_project_access(request, project, required="share")
    if payload.default_workflow_id is not None:
        await _require_project_workflow(
            request,
            workflow_id=payload.default_workflow_id,
            project_id=project_id,
        )
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("avatar_image_id"):
        try:
            fields[
                "avatar_image_id"
            ] = await request.app.state.project_management_service.avatar_snapshot(
                fields["avatar_image_id"],
                actor=require_current_user(request).email,
                project_owner=project.owner_email,
            )
        except ProjectManagementError as exc:
            raise api_exception(exc.status, exc.code, str(exc)) from exc
    if "avatar_image_id" in fields:
        fields["avatar_url"] = None
    mode = fields.pop("instructions_mode", "replace")
    if mode == "append" and isinstance(fields.get("instructions"), str):
        fields["instructions"] = append_project_instructions(
            project.instructions, fields["instructions"]
        )
    async with request.app.state.session_factory() as session:
        row = await update_project(session, project_id, **fields)
        await session.commit()
        if row is None:
            raise api_exception(404, "not_found", "Project not found")
        await session.refresh(row)
    return await _project_response(request, row)


@router.delete("/{project_id}", response_model=ProjectResponse)
async def archive_project_route(request: Request, project_id: str) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="share")
    async with request.app.state.session_factory() as session:
        row = await update_project(session, project_id, status="archived")
        await session.commit()
        if row is None:
            raise api_exception(404, "not_found", "Project not found")
        await session.refresh(row)
    return await _project_response(request, row)


@router.post("/{project_id}/sources", response_model=ProjectSourceResponse, status_code=201)
async def create_source_route(
    request: Request,
    project_id: str,
    payload: ProjectSourceCreateRequest,
) -> ProjectSourceResponse:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        row = await create_project_source(session, project_id=project_id, **payload.model_dump())
        await session.commit()
        await session.refresh(row)
    return project_source_to_response(row)


@router.patch("/{project_id}/sources/{source_id}", response_model=ProjectSourceResponse)
async def update_source_route(
    request: Request,
    project_id: str,
    source_id: str,
    payload: ProjectSourceUpdateRequest,
) -> ProjectSourceResponse:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        source = await get_project_source(session, source_id)
        if source is None or source.project_id != project_id:
            raise api_exception(404, "not_found", "Project source not found")
        row = await update_project_source(
            session, source_id, **payload.model_dump(exclude_unset=True)
        )
        await session.commit()
        if row is None:
            raise api_exception(404, "not_found", "Project source not found")
        await session.refresh(row)
    return project_source_to_response(row)


@router.delete("/{project_id}/sources/{source_id}", response_model=dict)
async def delete_source_route(request: Request, project_id: str, source_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        source = await get_project_source(session, source_id)
        if source is None or source.project_id != project_id:
            raise api_exception(404, "not_found", "Project source not found")
        ok = await delete_project_source(session, source_id)
        await session.commit()
    return {"ok": ok}


@router.post("/{project_id}/workflows/{workflow_id}", response_model=ProjectResponse)
async def attach_workflow_route(
    request: Request, project_id: str, workflow_id: str
) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    project = await _require_project(request, project_id, required="manage")
    await _require_project_workflow(
        request,
        workflow_id=workflow_id,
        project_id=project_id,
    )
    async with request.app.state.session_factory() as session:
        await attach_project_workflow(session, project_id, workflow_id)
        await session.commit()
    return await _project_response(request, project)


@router.delete("/{project_id}/workflows/{workflow_id}", response_model=ProjectResponse)
async def detach_workflow_route(
    request: Request, project_id: str, workflow_id: str
) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    project = await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        await detach_project_workflow(session, project_id, workflow_id)
        await session.commit()
    return await _project_response(request, project)


@router.get("/{project_id}/grants", response_model=list[ProjectGrantResponse])
async def list_grants_route(request: Request, project_id: str) -> list[ProjectGrantResponse]:
    project = await _require_project(request, project_id, required="view")
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = (
            await list_project_grants(session, project_id)
            if project.owner_email == user.email
            else await list_active_project_grants(session, project_id)
        )
    return [project_grant_to_response(row) for row in rows]


@router.post("/{project_id}/grants", response_model=ProjectGrantResponse, status_code=201)
async def create_grant_route(
    request: Request,
    project_id: str,
    payload: ProjectGrantCreateRequest,
) -> ProjectGrantResponse:
    forbid_mutation_for_viewer(request)
    if payload.grantee_type != "user":
        raise api_exception(400, "validation_error", "Only user grants are supported")
    try:
        row = await request.app.state.project_management_service.change_grant(
            project_id,
            require_current_user(request).email,
            grantee_email=payload.grantee_user_email,
            permission=payload.permission,
            note=payload.note,
            conflict_on_existing=True,
        )
    except ProjectManagementError as exc:
        raise api_exception(exc.status, exc.code, str(exc)) from exc
    return project_grant_to_response(row)


@router.patch("/{project_id}/grants/{grant_id}", response_model=ProjectGrantResponse)
async def update_grant_route(
    request: Request,
    project_id: str,
    grant_id: str,
    payload: ProjectGrantUpdateRequest,
) -> ProjectGrantResponse:
    forbid_mutation_for_viewer(request)
    try:
        row = await request.app.state.project_management_service.change_grant(
            project_id,
            require_current_user(request).email,
            grant_id=grant_id,
            permission=payload.permission,
        )
    except ProjectManagementError as exc:
        raise api_exception(exc.status, exc.code, str(exc)) from exc
    return project_grant_to_response(row)


@router.post("/{project_id}/leave", response_model=dict)
async def leave_project_route(
    request: Request, project_id: str, payload: ProjectLeaveRequest | None = None
) -> dict[str, Any]:
    """Leave a shared project; optionally make the caller's project work private first."""

    try:
        result: dict[str, Any] = await request.app.state.project_management_service.leave(
            project_id,
            require_current_user(request).email,
            make_private=payload.make_private if payload else False,
        )
        return result
    except ProjectManagementError as exc:
        raise api_exception(exc.status, exc.code, str(exc)) from exc


@router.delete("/{project_id}/grants/{grant_id}", response_model=dict)
async def revoke_grant_route(request: Request, project_id: str, grant_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    try:
        await request.app.state.project_management_service.change_grant(
            project_id, require_current_user(request).email, grant_id=grant_id, revoke=True
        )
    except ProjectManagementError as exc:
        raise api_exception(exc.status, exc.code, str(exc)) from exc
    return {"ok": True}


@router.get("/{project_id}/agents", response_model=list[ProjectAgentResponse])
async def list_project_agents_route(
    request: Request, project_id: str
) -> list[ProjectAgentResponse]:
    await _require_project(request, project_id, required="view")
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_project_agents(session, project_id)
    return [
        project_agent_to_response(assignment, agent, viewer_email=user.email)
        for assignment, agent in rows
    ]


@router.put("/{project_id}/agents/{agent_id}", response_model=ProjectAgentResponse)
async def upsert_project_agent_route(
    request: Request,
    project_id: str,
    agent_id: str,
    payload: ProjectAgentUpsertRequest,
) -> ProjectAgentResponse:
    """Assign an agent to a project or update its assignment.

    Requires the manager role on the project and ownership of the agent:
    assigning shares the agent with every project member, so only its owner
    may decide that, including whose executor it runs on.
    """

    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    project = await _require_project(request, project_id, required="manage")
    if project.status != "active":
        raise api_exception(409, "conflict", "Project is not active")
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
        if agent is None:
            raise api_exception(404, "not_found", "Agent not found")
        if agent.owner_email != user.email or getattr(agent, "agent_type", "primary") != "primary":
            raise api_exception(
                403, "forbidden", "Only the owner of a primary agent can assign it to a project"
            )
        assignment = await upsert_project_agent(
            session,
            project_id=project_id,
            agent_id=agent_id,
            added_by=user.email,
            executor_scope=payload.executor_scope,
            is_default=payload.is_default,
        )
        await session.commit()
        await session.refresh(assignment)
    return project_agent_to_response(assignment, agent, viewer_email=user.email)


@router.delete("/{project_id}/agents/{agent_id}", response_model=dict)
async def remove_project_agent_route(
    request: Request, project_id: str, agent_id: str
) -> dict[str, bool]:
    """Remove an assignment (agent owner with manager role, or project owner)."""

    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    project = await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        assignment = await get_project_agent(session, project_id, agent_id)
        if assignment is None:
            raise api_exception(404, "not_found", "Project agent not found")
        agent = await get_agent(session, agent_id)
        agent_owner = getattr(agent, "owner_email", None)
        if user.email not in {agent_owner, project.owner_email}:
            raise api_exception(
                403, "forbidden", "Only the agent owner or project owner can remove it"
            )
        ok = await remove_project_agent(session, project_id, agent_id)
        await session.commit()
    return {"ok": ok}


@router.get("/{project_id}/conversations", response_model=list[ConversationResponse])
async def list_project_conversations_route(
    request: Request,
    project_id: str,
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=2048),
    topics_only: bool = Query(default=False),
) -> list[ConversationResponse]:
    """Readable project conversations from every member, newest first."""

    await _require_project(request, project_id, required="view")
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        default_agent_ids = await list_default_agent_ids_for_project(session, project_id)
        rows = await list_project_conversations(
            session,
            project_id,
            viewer_email=user.email,
            default_agent_ids=default_agent_ids,
            agent_id=agent_id,
            limit=limit,
            before=decode_project_cursor(
                cursor,
                [project_id, user.email, "topics" if topics_only else "conversations", agent_id],
            ),
            topics_only=topics_only,
        )
        own_ids = [row.conversation_id for row in rows if row.user_email == user.email]
        scheduler = getattr(request.app.state, "turn_scheduler", None)
        active_turns = {}
        if scheduler is not None and own_ids:
            active_turns = await scheduler.durable_running_turn_states(own_ids, session=session)
        return await conversation_list_summaries(
            session,
            rows,
            user_email=user.email,
            can_mutate=user.role != "viewer",
            active_turn_states=active_turns,
            include_attention_actions=True,
        )


@router.get("/{project_id}/tasks", response_model=list[ProjectTaskSummaryResponse])
async def list_project_tasks_route(
    request: Request,
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=2048),
) -> list[ProjectTaskSummaryResponse]:
    await _require_project(request, project_id, required="view")
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_project_tasks(
            session,
            project_id,
            viewer_email=user.email,
            limit=limit,
            before=decode_project_cursor(cursor, [project_id, user.email, "tasks"]),
        )
    return [
        ProjectTaskSummaryResponse(
            task_id=row.task_id,
            title=row.title,
            status=row.status,
            agent_id=row.agent_id,
            workflow_id=row.workflow_id,
            created_by=row.created_by,
            visibility=str(getattr(row, "visibility", None) or "private"),
            is_readonly_for_caller=row.created_by != user.email,
            created_at=row.created_at,
            updated_at=row.updated_at,
            priority=row.priority,
            source_type=row.source_type,
            source_ref=row.source_ref,
            started_at=row.started_at,
            completed_at=row.completed_at,
            result_summary=row.result_summary,
        )
        for row in rows
    ]


@router.get("/{project_id}/conversations/page", response_model=CursorPage[ConversationResponse])
async def project_conversations_page(
    request: Request,
    project_id: str,
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
) -> CursorPage[ConversationResponse]:
    """Page the same authorized projection used by the legacy list endpoint."""
    rows = await list_project_conversations_route(
        request, project_id, agent_id, limit + 1, cursor, True
    )
    items = rows[:limit]
    has_more = len(rows) > limit
    last = items[-1] if items else None
    at = (last.last_message_at or last.created_at) if last else None
    next_cursor = (
        encode_project_cursor(
            [project_id, require_current_user(request).email, "topics", agent_id],
            at,
            last.conversation_id,
        )
        if has_more and last and at
        else None
    )
    return CursorPage(items=items, cursor=next_cursor, has_more=bool(next_cursor))


@router.get("/{project_id}/tasks/page", response_model=CursorPage[ProjectTaskSummaryResponse])
async def project_tasks_page(
    request: Request,
    project_id: str,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
) -> CursorPage[ProjectTaskSummaryResponse]:
    """Page shared tasks without exposing their author's runtime projections."""
    rows = await list_project_tasks_route(request, project_id, limit + 1, cursor)
    items = rows[:limit]
    has_more = len(rows) > limit
    last = items[-1] if items else None
    next_cursor = (
        encode_project_cursor(
            [project_id, require_current_user(request).email, "tasks"],
            last.updated_at,
            last.task_id,
        )
        if has_more and last and last.updated_at
        else None
    )
    return CursorPage(items=items, cursor=next_cursor, has_more=bool(next_cursor))


@router.get("/{project_id}/knowledgebases", response_model=list[KnowledgebaseModel])
async def project_knowledgebases(request: Request, project_id: str) -> list[KnowledgebaseModel]:
    """List project-shared knowledgebases after fresh membership checks."""
    user = require_current_user(request)
    await _require_project(request, project_id, required="view")
    async with request.app.state.session_factory() as db:
        rows = await list_project_knowledgebases(db, user_email=user.email, project_id=project_id)
    return [project_knowledgebase_metadata(row, user.email) for row in rows]


@router.put("/{project_id}/knowledgebases/{knowledgebase_id}")
async def assign_project_knowledgebase(
    request: Request, project_id: str, knowledgebase_id: str
) -> dict[str, bool]:
    """Assign an owned knowledgebase to an administrable project."""
    return await _change_project_knowledgebase(request, project_id, knowledgebase_id, True)


@router.delete("/{project_id}/knowledgebases/{knowledgebase_id}")
async def unassign_project_knowledgebase(
    request: Request, project_id: str, knowledgebase_id: str
) -> dict[str, bool]:
    """Revoke assignment without deleting knowledgebase content."""
    return await _change_project_knowledgebase(request, project_id, knowledgebase_id, False)


async def _change_project_knowledgebase(
    request: Request, project_id: str, knowledgebase_id: str, assigned: bool
) -> dict[str, bool]:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    try:
        async with request.app.state.session_factory() as db:
            await set_project_knowledgebase(
                db,
                project_id=project_id,
                knowledgebase_id=knowledgebase_id,
                actor_email=user.email,
                assigned=assigned,
            )
            await db.commit()
    except PermissionError as exc:
        raise api_exception(403, "forbidden", str(exc)) from exc
    except ValueError as exc:
        raise api_exception(404, "not_found", str(exc)) from exc
    except IntegrityError:
        raise api_exception(409, "conflict", "Assignment changed concurrently") from None
    return {"assigned": assigned}


@router.get("/{project_id}/activity", response_model=ProjectActivityResponse)
async def project_activity_route(
    request: Request,
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> ProjectActivityResponse:
    """Recent deliverables and artifacts from readable project work."""

    await _require_project(request, project_id, required="view")
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        deliverables = await list_project_deliverables(
            session, project_id, viewer_email=user.email, limit=limit
        )
        artifacts = await list_project_artifacts(
            session, project_id, viewer_email=user.email, limit=limit
        )
    # Previews are optional and bounded; unavailable payloads must not hide work.
    for row, _task_id in deliverables:
        if sum((row.content_size or 0, row.rich_size or 0, row.outputs_size or 0)) > 262144:
            continue
        try:
            await hydrate_deliverable_payload(row, request.app.state.artifact_store)
        except Exception:
            logging.getLogger(__name__).warning(
                "Project deliverable preview unavailable: %s", row.deliverable_id
            )
    return ProjectActivityResponse(
        deliverables=[
            ProjectDeliverableItem(
                deliverable_id=row.deliverable_id,
                title=row.title,
                format=row.format,
                status=row.status,
                conversation_id=row.conversation_id,
                task_id=task_id,
                step_run_id=row.step_run_id,
                is_readonly_for_caller=row.owner_email != user.email,
                content_preview=(row.content or "")[:600] or None,
                created_at=row.created_at,
                lineage_id=row.lineage_id or row.deliverable_id,
                revision=int(row.revision or 1),
            )
            for row, task_id in deliverables
        ],
        artifacts=[
            ProjectArtifactItem(
                artifact_id=row.artifact_id,
                filename=row.filename,
                mime_type=row.mime_type,
                size_bytes=int(row.size_bytes or 0),
                kind=row.kind,
                purpose=row.purpose,
                conversation_id=row.conversation_id,
                owner_email=row.owner_email,
                created_at=row.created_at,
                lineage_id=row.lineage_id or row.artifact_id,
                revision=int(row.revision or 1),
            )
            for row in artifacts
        ],
    )


@router.post("/{project_id}/avatar/generate", response_model=ProjectAvatarGenerateResponse)
async def generate_project_avatar(
    request: Request, project_id: str
) -> ProjectAvatarGenerateResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    project = await _require_project(request, project_id, required="manage")
    image_gen = getattr(request.app.state.providers, "image_generation", None)
    if image_gen is None:
        raise api_exception(501, "not_implemented", "Image generation provider is unavailable")
    prompt = f"Project avatar for {project.name}. {project.description or ''}".strip()
    result = await image_gen.image_generate(prompt=prompt, task_type="image_generation", n=1)
    if not getattr(result, "images", None):
        raise api_exception(502, "provider_error", "No images returned by the model")
    from cognis.api.routes.images import _image_bytes

    img = result.images[0]
    artifact_store = request.app.state.artifact_store
    image_id = artifact_store.generate_id("img")
    await artifact_store.async_save(
        "avatars",
        image_id,
        "image",
        await _image_bytes(img),
        img.content_type,
        owner_email=user.email,
    )
    async with request.app.state.session_factory() as session:
        await update_project(session, project_id, avatar_image_id=image_id)
        await session.commit()
    return ProjectAvatarGenerateResponse(
        avatar_image_id=image_id, avatar_url=f"/api/v1/images/{image_id}"
    )
