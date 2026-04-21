"""Workflow routes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    paginate_items,
    require_current_user,
)
from cognis.api.models import (
    CursorPage,
    StepProfileResponse,
    WorkflowRequest,
    WorkflowResponse,
    WorkflowUpdateRequest,
)
from cognis.api.serializers import workflow_to_response
from cognis.core.workflow_management import (
    create_user_workflow,
    delete_user_workflow,
    duplicate_visible_workflow,
    update_user_workflow,
)
from cognis.models.config import NORMALIZED_REASONING_LEVELS, normalize_reasoning_level
from cognis.models.workflow import StepProfileConfig
from cognis.store.queries import (
    delete_system_workflow_override,
    get_system_workflow_override,
    upsert_system_workflow_override,
)

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


@router.get("/step-profiles", response_model=list[StepProfileResponse])
async def workflow_step_profiles(request: Request) -> list[StepProfileResponse]:
    require_current_user(request)
    registry = request.app.state.step_profile_registry
    return [
        StepProfileResponse(
            profile_id=definition.profile_id,
            name=definition.name,
            mode=str(definition.mode),
            config=definition.config.model_dump(mode="json"),
            has_override=registry.has_override(definition.profile_id),
            is_custom=registry.is_custom(definition.profile_id),
        )
        for definition in registry.list_definitions()
    ]


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
    include_disabled: bool = Query(default=False),
    include_ephemeral: bool = Query(default=False),
) -> CursorPage[WorkflowResponse]:
    user = require_current_user(request)
    workflows = await request.app.state.workflow_registry.list_all(
        owner_email=user.email,
        include_disabled=include_disabled,
        include_ephemeral=include_ephemeral,
    )
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
    _validate_workflow_payload(payload.model_dump(mode="json"))
    try:
        row = await create_user_workflow(
            session_factory=request.app.state.session_factory,
            owner_email=user.email,
            payload=payload.model_dump(mode="json"),
        )
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    return workflow_to_response(row)


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def workflow_detail(request: Request, workflow_id: str) -> WorkflowResponse:
    user = require_current_user(request)
    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=user.email, include_disabled=True
    )
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
    base = request.app.state.workflow_registry.get_system_workflow(workflow_id)
    if base is not None:
        return await _update_system_workflow_route(request, workflow_id, payload)
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
        if message == "Ephemeral lifecycle is reserved for composed workflows":
            raise api_exception(400, "validation_error", message) from exc
        raise api_exception(409, "conflict", message) from exc
    return workflow_to_response(row)


async def _update_system_workflow_route(
    request: Request,
    workflow_id: str,
    payload: WorkflowUpdateRequest,
) -> WorkflowResponse:
    user = require_current_user(request)
    base = request.app.state.workflow_registry.get_system_workflow(workflow_id)
    if base is None:
        raise api_exception(404, "not_found", "Workflow not found")
    if not base.allow_user_override:
        raise api_exception(403, "forbidden", "This system workflow cannot be overridden")

    updates = payload.model_dump(exclude_unset=True)
    forbidden = sorted(key for key in updates if key != "steps")
    if forbidden:
        raise api_exception(
            403,
            "forbidden",
            f"System workflow overrides only allow step runtime tuning: {', '.join(forbidden)}",
        )

    step_overrides: dict[str, dict[str, object]] = {}
    for step_payload in updates.get("steps") or []:
        if not isinstance(step_payload, dict):
            continue
        step_name = step_payload.get("name")
        if not isinstance(step_name, str) or not step_name:
            continue
        override: dict[str, object] = {}
        reasoning_effort = step_payload.get("reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            normalized_effort = normalize_reasoning_level(reasoning_effort)
            if normalized_effort is None:
                allowed = ", ".join(NORMALIZED_REASONING_LEVELS)
                raise api_exception(
                    status_code=422,
                    code="invalid_reasoning_effort",
                    message=(
                        f"Step {step_name!r} reasoning_effort must be one of "
                        f"{allowed}; got {reasoning_effort!r}."
                    ),
                )
            if normalized_effort:
                override["reasoning_effort"] = normalized_effort
        completion = step_payload.get("completion")
        if isinstance(completion, dict):
            max_attempts = completion.get("max_attempts")
            if isinstance(max_attempts, int):
                override["completion"] = {"max_attempts": max_attempts}
        step_profile_id = step_payload.get("step_profile_id")
        if isinstance(step_profile_id, str):
            override["step_profile_id"] = step_profile_id
        step_profile_mode = step_payload.get("step_profile_mode")
        if isinstance(step_profile_mode, str) and step_profile_mode in {"soft", "hard"}:
            override["step_profile_mode"] = step_profile_mode
        step_profile = step_payload.get("step_profile")
        if isinstance(step_profile, dict):
            try:
                override["step_profile"] = StepProfileConfig.model_validate(step_profile).model_dump(
                    mode="json"
                )
            except Exception as exc:
                raise api_exception(422, "invalid_step_profile", str(exc)) from exc
        if override:
            step_overrides[step_name] = override

    async with request.app.state.session_factory() as session:
        existing = await get_system_workflow_override(
            session, owner_email=user.email, workflow_id=workflow_id
        )
        await upsert_system_workflow_override(
            session,
            owner_email=user.email,
            workflow_id=workflow_id,
            disabled=(existing.disabled if existing else False),
            step_overrides=step_overrides or None,
        )
        await session.commit()

    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=user.email, include_disabled=True
    )
    assert workflow is not None
    return workflow_to_response(workflow)


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
    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=user.email, include_disabled=True
    )
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


@router.post("/{workflow_id}/reset-overrides", response_model=dict)
async def workflow_reset_overrides(request: Request, workflow_id: str) -> dict[str, bool]:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    base = request.app.state.workflow_registry.get_system_workflow(workflow_id)
    if base is None:
        raise api_exception(404, "not_found", "Workflow not found")
    async with request.app.state.session_factory() as session:
        ok = await delete_system_workflow_override(
            session, owner_email=user.email, workflow_id=workflow_id
        )
        await session.commit()
    return {"ok": ok}


@router.post("/{workflow_id}/disable", response_model=WorkflowResponse)
async def workflow_disable(request: Request, workflow_id: str) -> WorkflowResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    base = request.app.state.workflow_registry.get_system_workflow(workflow_id)
    if base is None:
        raise api_exception(404, "not_found", "Workflow not found")
    if not base.allow_user_disable:
        raise api_exception(403, "forbidden", "This system workflow cannot be disabled")
    async with request.app.state.session_factory() as session:
        existing = await get_system_workflow_override(
            session, owner_email=user.email, workflow_id=workflow_id
        )
        await upsert_system_workflow_override(
            session,
            owner_email=user.email,
            workflow_id=workflow_id,
            disabled=True,
            step_overrides=(existing.step_overrides if existing else None),
        )
        await session.commit()
    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=user.email, include_disabled=True
    )
    assert workflow is not None
    return workflow_to_response(workflow)


@router.post("/{workflow_id}/enable", response_model=WorkflowResponse)
async def workflow_enable(request: Request, workflow_id: str) -> WorkflowResponse:
    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    base = request.app.state.workflow_registry.get_system_workflow(workflow_id)
    if base is None:
        raise api_exception(404, "not_found", "Workflow not found")
    async with request.app.state.session_factory() as session:
        existing = await get_system_workflow_override(
            session, owner_email=user.email, workflow_id=workflow_id
        )
        await upsert_system_workflow_override(
            session,
            owner_email=user.email,
            workflow_id=workflow_id,
            disabled=False,
            step_overrides=(existing.step_overrides if existing else None),
        )
        await session.commit()
    workflow = await request.app.state.workflow_registry.get(
        workflow_id, owner_email=user.email, include_disabled=True
    )
    assert workflow is not None
    return workflow_to_response(workflow)
