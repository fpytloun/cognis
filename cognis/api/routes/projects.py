"""Project CRUD routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    apply_project_access_metadata,
    check_project_access,
    forbid_mutation_for_viewer,
    require_current_user,
)
from cognis.api.models import (
    ProjectAvatarGenerateResponse,
    ProjectCreateRequest,
    ProjectGrantCreateRequest,
    ProjectGrantResponse,
    ProjectResponse,
    ProjectSourceCreateRequest,
    ProjectSourceResponse,
    ProjectSourceUpdateRequest,
    ProjectUpdateRequest,
)
from cognis.api.serializers import (
    project_grant_to_response,
    project_source_to_response,
    project_to_response,
)
from cognis.store.queries import (
    attach_project_workflow,
    create_project,
    create_project_grant,
    create_project_source,
    delete_project_source,
    detach_project_workflow,
    get_project,
    get_project_grant,
    get_project_source,
    list_project_grants,
    list_project_sources,
    list_project_workflow_ids,
    list_projects_for_user,
    revoke_project_grant,
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


async def _project_response(request: Request, project: Any, *, include_grants: bool = False) -> ProjectResponse:
    async with request.app.state.session_factory() as session:
        sources = await list_project_sources(session, project.project_id)
        workflow_ids = await list_project_workflow_ids(session, project.project_id)
        grants = await list_project_grants(session, project.project_id) if include_grants else []
    return project_to_response(project, sources=sources, workflow_ids=workflow_ids, grants=grants)


async def _require_project_workflow(request: Request, *, workflow_id: str, project_id: str) -> None:
    """Validate a workflow can be used by the caller's project."""

    user = require_current_user(request)
    workflow = await request.app.state.workflow_registry.get(
        workflow_id,
        owner_email=user.email,
        project_id=project_id,
    )
    if workflow is None:
        raise api_exception(404, "not_found", "Workflow not found")
    owner_email = getattr(workflow, "owner_email", None)
    is_system = bool(getattr(workflow, "is_system", False))
    if not is_system and owner_email != user.email:
        raise api_exception(404, "not_found", "Workflow not found")


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    request: Request,
    status: str | None = Query(default="active"),
    q: str | None = Query(default=None),
) -> list[ProjectResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_projects_for_user(session, user.email, status=status, query=q)
    result: list[ProjectResponse] = []
    for row in rows:
        access = await check_project_access(request, row, required="view")
        result.append(await _project_response(request, apply_project_access_metadata(row, access)))
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
    async with request.app.state.session_factory() as session:
        row = await create_project(
            session,
            owner_email=user.email,
            name=payload.name,
            description=payload.description,
            instructions=payload.instructions,
            default_workflow_id=payload.default_workflow_id,
            avatar_image_id=payload.avatar_image_id,
            avatar_url=payload.avatar_url,
            metadata=payload.metadata,
        )
        await session.commit()
        await session.refresh(row)
    return await _project_response(request, row)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project_route(request: Request, project_id: str) -> ProjectResponse:
    project = await _require_project(request, project_id)
    return await _project_response(request, project, include_grants=project.owner_email == require_current_user(request).email)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project_route(
    request: Request,
    project_id: str,
    payload: ProjectUpdateRequest,
) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="manage")
    if payload.default_workflow_id is not None:
        await _require_project_workflow(
            request,
            workflow_id=payload.default_workflow_id,
            project_id=project_id,
        )
    async with request.app.state.session_factory() as session:
        row = await update_project(session, project_id, **payload.model_dump(exclude_unset=True))
        await session.commit()
        if row is None:
            raise api_exception(404, "not_found", "Project not found")
        await session.refresh(row)
    return await _project_response(request, row)


@router.delete("/{project_id}", response_model=ProjectResponse)
async def archive_project_route(request: Request, project_id: str) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="manage")
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
        row = await update_project_source(session, source_id, **payload.model_dump(exclude_unset=True))
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
async def attach_workflow_route(request: Request, project_id: str, workflow_id: str) -> ProjectResponse:
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
async def detach_workflow_route(request: Request, project_id: str, workflow_id: str) -> ProjectResponse:
    forbid_mutation_for_viewer(request)
    project = await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        await detach_project_workflow(session, project_id, workflow_id)
        await session.commit()
    return await _project_response(request, project)


@router.get("/{project_id}/grants", response_model=list[ProjectGrantResponse])
async def list_grants_route(request: Request, project_id: str) -> list[ProjectGrantResponse]:
    await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        rows = await list_project_grants(session, project_id)
    return [project_grant_to_response(row) for row in rows]


@router.post("/{project_id}/grants", response_model=ProjectGrantResponse, status_code=201)
async def create_grant_route(
    request: Request,
    project_id: str,
    payload: ProjectGrantCreateRequest,
) -> ProjectGrantResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    await _require_project(request, project_id, required="manage")
    if payload.grantee_type == "user" and not payload.grantee_user_email:
        raise api_exception(400, "validation_error", "grantee_user_email is required")
    async with request.app.state.session_factory() as session:
        row = await create_project_grant(
            session,
            project_id=project_id,
            granted_by=user.email,
            **payload.model_dump(),
        )
        await session.commit()
        await session.refresh(row)
    return project_grant_to_response(row)


@router.delete("/{project_id}/grants/{grant_id}", response_model=dict)
async def revoke_grant_route(request: Request, project_id: str, grant_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    await _require_project(request, project_id, required="manage")
    async with request.app.state.session_factory() as session:
        grant = await get_project_grant(session, grant_id)
        if grant is None or grant.project_id != project_id:
            raise api_exception(404, "not_found", "Project grant not found")
        ok = await revoke_project_grant(session, grant_id)
        await session.commit()
    return {"ok": ok}


@router.post("/{project_id}/avatar/generate", response_model=ProjectAvatarGenerateResponse)
async def generate_project_avatar(request: Request, project_id: str) -> ProjectAvatarGenerateResponse:
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
    return ProjectAvatarGenerateResponse(avatar_image_id=image_id, avatar_url=f"/api/v1/images/{image_id}")
