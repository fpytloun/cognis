"""Workflow routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
)
from cognis.api.models import CursorPage, WorkflowRequest, WorkflowResponse, WorkflowUpdateRequest
from cognis.api.serializers import workflow_to_response
from cognis.store.queries import (
    create_workflow,
    delete_workflow,
    get_workflow,
    update_workflow,
)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _parse_step_inputs(step: dict[str, object]) -> list[str]:
    raw_input = step.get("input", [])
    if isinstance(raw_input, list):
        return [value for value in raw_input if isinstance(value, str) and value.strip()]
    return []


def _validate_workflow_steps(steps: list[dict[str, object]]) -> None:
    if not steps:
        raise api_exception(400, "validation_error", "Workflow must contain at least one step")

    seen_names: set[str] = set()
    prior_names: list[str] = []
    for index, step in enumerate(steps, start=1):
        name = step.get("name")
        if not isinstance(name, str) or not name.strip():
            raise api_exception(400, "validation_error", f"Workflow step {index} is missing a name")
        if name in seen_names:
            raise api_exception(400, "validation_error", f"Duplicate workflow step: {name}")
        for input_name in _parse_step_inputs(step):
            if input_name not in prior_names:
                raise api_exception(
                    400,
                    "validation_error",
                    f"Workflow step {name} references missing or later input: {input_name}",
                )
        seen_names.add(name)
        prior_names.append(name)


def _validate_workflow_payload(definition: dict[str, object]) -> None:
    steps = definition.get("steps")
    if not isinstance(steps, list):
        raise api_exception(400, "validation_error", "Workflow steps must be a list")
    if not all(isinstance(step, dict) for step in steps):
        raise api_exception(400, "validation_error", "Workflow steps must be objects")
    _validate_workflow_steps([step for step in steps if isinstance(step, dict)])


@router.get("", response_model=CursorPage[WorkflowResponse])
async def workflow_list(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> CursorPage[WorkflowResponse]:
    user = require_current_user(request)
    workflows = await request.app.state.workflow_registry.list_all(owner_email=user.email)
    items = [workflow_to_response(workflow) for workflow in workflows]
    page_items, next_cursor, has_more = paginate_items(
        items,
        limit=limit,
        cursor=cursor,
        get_item_id=lambda item: item.workflow_id,
    )
    return CursorPage(items=page_items, cursor=next_cursor, has_more=has_more)


@router.post("", response_model=WorkflowResponse)
async def workflow_create(request: Request, payload: WorkflowRequest) -> WorkflowResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    workflow_id = payload.workflow_id or f"wf_{uuid.uuid4().hex[:12]}"
    definition = {
        "workflow_id": workflow_id,
        "name": payload.name,
        "description": payload.description,
        "version": payload.version,
        "criteria": payload.criteria,
        "tags": payload.tags,
        "interaction": payload.interaction,
        "defaults": payload.defaults,
        "steps": payload.steps,
        "is_system": False,
        "owner_email": user.email,
    }
    _validate_workflow_payload(definition)
    async with request.app.state.session_factory() as session:
        row = await create_workflow(
            session,
            workflow_id=workflow_id,
            name=payload.name,
            description=payload.description,
            definition=definition,
            version=payload.version,
            is_system=False,
            owner_email=user.email,
        )
        await session.commit()
        await session.refresh(row)
    return workflow_to_response(row)


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def workflow_detail(request: Request, workflow_id: str) -> WorkflowResponse:
    user = require_current_user(request)
    workflow = await request.app.state.workflow_registry.get(workflow_id)
    if workflow is None:
        raise api_exception(404, "not_found", "Workflow not found")
    if (
        not workflow.is_system
        and workflow.owner_email not in {user.email, None}
        and user.role != "admin"
    ):
        raise api_exception(403, "forbidden", "Workflow access denied")
    return workflow_to_response(workflow)


@router.put("/{workflow_id}", response_model=WorkflowResponse)
async def workflow_update_route(
    request: Request,
    workflow_id: str,
    payload: WorkflowUpdateRequest,
) -> WorkflowResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise api_exception(404, "not_found", "Workflow not found")
        if row.is_system:
            raise api_exception(403, "forbidden", "System workflows are read-only")
        if row.owner_email != user.email:
            raise api_exception(403, "forbidden", "Workflow access denied")
        definition = dict(row.definition or {})
        updates = payload.model_dump(exclude_none=True)
        definition.update(updates)
        _validate_workflow_payload(definition)
        ok = await update_workflow(
            session,
            workflow_id,
            updates={
                **({"name": payload.name} if payload.name is not None else {}),
                **({"description": payload.description} if payload.description is not None else {}),
                **({"version": payload.version} if payload.version is not None else {}),
                "definition": definition,
            },
        )
        if not ok:
            raise api_exception(400, "validation_error", "Workflow update failed")
        await session.commit()
        await session.refresh(row)
    return workflow_to_response(row)


@router.delete("/{workflow_id}", response_model=dict)
async def workflow_delete_route(request: Request, workflow_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_workflow(session, workflow_id)
        if row is None:
            raise api_exception(404, "not_found", "Workflow not found")
        if row.is_system:
            raise api_exception(403, "forbidden", "System workflows are read-only")
        if row.owner_email != user.email:
            raise api_exception(403, "forbidden", "Workflow access denied")
        ok = await delete_workflow(session, workflow_id)
        await session.commit()
    return {"ok": ok}


@router.post("/{workflow_id}/duplicate", response_model=WorkflowResponse)
async def workflow_duplicate(request: Request, workflow_id: str) -> WorkflowResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    workflow = await request.app.state.workflow_registry.get(workflow_id)
    if workflow is None:
        raise api_exception(404, "not_found", "Workflow not found")
    if (
        not workflow.is_system
        and workflow.owner_email not in {user.email, None}
        and user.role != "admin"
    ):
        raise api_exception(403, "forbidden", "Workflow access denied")

    new_workflow_id = f"wf_{uuid.uuid4().hex[:12]}"
    definition = workflow.model_dump(mode="json")
    definition["workflow_id"] = new_workflow_id
    definition["name"] = f"{workflow.name} Copy"
    definition["is_system"] = False
    definition["owner_email"] = user.email
    _validate_workflow_payload(definition)

    async with request.app.state.session_factory() as session:
        new_row = await create_workflow(
            session,
            workflow_id=new_workflow_id,
            name=f"{workflow.name} Copy",
            description=workflow.description or "",
            definition=definition,
            version=workflow.version,
            is_system=False,
            owner_email=user.email,
        )
        await session.commit()
        await session.refresh(new_row)
    return workflow_to_response(new_row)
