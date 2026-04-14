"""Workflow routes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
)
from cognis.api.models import CursorPage, WorkflowRequest, WorkflowResponse, WorkflowUpdateRequest
from cognis.api.serializers import workflow_to_response
from cognis.core.workflow_management import (
    create_user_workflow,
    delete_user_workflow,
    duplicate_visible_workflow,
    update_user_workflow,
)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


def _validate_workflow_payload(definition: dict[str, object]) -> None:
    """Validate a workflow payload by parsing through the domain model.

    Uses ``Workflow.model_validate`` to leverage Pydantic coercion (including
    backward-compatible ``list[str]`` → ``StepInputConfig``) and then runs the
    shared registry validation for reference integrity.
    """
    from cognis.core.workflow_registry import _validate_workflow
    from cognis.models.workflow import Workflow

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        raise api_exception(400, "validation_error", "Workflow must contain at least one step")

    try:
        workflow = Workflow.model_validate(definition)
    except Exception as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    try:
        _validate_workflow(workflow)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc


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
    row = await create_user_workflow(
        session_factory=request.app.state.session_factory,
        owner_email=user.email,
        payload=payload.model_dump(mode="json"),
    )
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
    try:
        row = await update_user_workflow(
            session_factory=request.app.state.session_factory,
            workflow_id=workflow_id,
            owner_email=user.email,
            payload=payload.model_dump(exclude_none=True, mode="json"),
        )
    except ValueError as exc:
        message = str(exc)
        if message == "Workflow not found":
            raise api_exception(404, "not_found", message) from exc
        if message in {"System workflows are read-only", "Workflow access denied"}:
            raise api_exception(403, "forbidden", message) from exc
        raise api_exception(409, "conflict", message) from exc
    return workflow_to_response(row)


@router.delete("/{workflow_id}", response_model=dict)
async def workflow_delete_route(request: Request, workflow_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    try:
        ok = await delete_user_workflow(
            session_factory=request.app.state.session_factory,
            workflow_id=workflow_id,
            owner_email=user.email,
        )
    except ValueError as exc:
        message = str(exc)
        if message == "Workflow not found":
            raise api_exception(404, "not_found", message) from exc
        if message in {"System workflows are read-only", "Workflow access denied"}:
            raise api_exception(403, "forbidden", message) from exc
        raise api_exception(409, "conflict", message) from exc
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

    new_row = await duplicate_visible_workflow(
        session_factory=request.app.state.session_factory,
        workflow_registry=request.app.state.workflow_registry,
        workflow_id=workflow_id,
        owner_email=user.email,
        allow_admin=user.role == "admin",
    )
    return workflow_to_response(new_row)
