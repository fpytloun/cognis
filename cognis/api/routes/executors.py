"""Executor configuration CRUD routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user
from cognis.api.executor_runtime import schedule_executor_reconfigure
from cognis.api.models import (
    ExecutorConfigResponse,
    ExecutorCreateRequest,
    ExecutorTokenResponse,
    ExecutorUpdateRequest,
)
from cognis.core.executor_policy import (
    ensure_executor_type_allowed,
    load_executor_policy,
    validate_executor_mcp_scope,
)
from cognis.store.queries import (
    create_executor,
    delete_executor,
    get_executor_row,
    list_executors,
    update_executor,
    update_executor_runtime_state,
)

router = APIRouter(tags=["executors"])


def _executor_to_response(row: Any) -> ExecutorConfigResponse:
    return ExecutorConfigResponse(
        executor_id=row.executor_id,
        name=row.name,
        executor_type=row.executor_type,
        labels=row.labels or {},
        enabled_tools=row.enabled_tools or [],
        enabled_tool_groups=row.enabled_tool_groups or [],
        config=row.config or {},
        status=row.status,
        runtime_state=getattr(row, "runtime_state", "offline"),
        desired_config_version=getattr(row, "desired_config_version", 0),
        applied_config_version=getattr(row, "applied_config_version", 0),
        runtime_metadata=getattr(row, "runtime_metadata", None) or {},
        last_observed_at=getattr(row, "last_observed_at", None),
        is_default=row.is_default,
        owner_email=row.owner_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/v1/executors", response_model=list[ExecutorConfigResponse])
async def list_executors_route(request: Request) -> list[ExecutorConfigResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_executors(session, owner_email=user.email)
    return [_executor_to_response(row) for row in rows]


@router.get("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def get_executor_route(request: Request, executor_id: str) -> ExecutorConfigResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id, owner_email=user.email)
    if row is None:
        raise api_exception(404, "not_found", "Executor not found")
    return _executor_to_response(row)


@router.post("/api/v1/executors", response_model=ExecutorConfigResponse, status_code=201)
async def create_executor_route(
    request: Request, body: ExecutorCreateRequest
) -> ExecutorConfigResponse:
    user = require_current_user(request)
    policy = await load_executor_policy(request.app.state.session_factory)
    try:
        ensure_executor_type_allowed(body.executor_type, policy)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    async with request.app.state.session_factory() as session:
        try:
            await validate_executor_mcp_scope(
                session,
                owner_email=user.email,
                config=body.config or None,
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        row = await create_executor(
            session,
            executor_id=body.executor_id,
            name=body.name,
            executor_type=body.executor_type,
            labels=body.labels or None,
            enabled_tools=body.enabled_tools,
            enabled_tool_groups=body.enabled_tool_groups,
            config=body.config or None,
            is_default=body.is_default,
            owner_email=user.email,
        )
        if row.executor_type == "websocket":
            row.desired_config_version = 1
        await session.commit()
    return _executor_to_response(row)


@router.post("/api/v1/executors/{executor_id}/token", response_model=ExecutorTokenResponse)
async def generate_executor_token_route(
    request: Request,
    executor_id: str,
) -> ExecutorTokenResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id, owner_email=user.email)
    if row is None:
        raise api_exception(404, "not_found", "Executor not found")
    token = request.app.state.providers.auth.sign_executor_token(executor_id)
    return ExecutorTokenResponse(executor_id=executor_id, token=token, expires_in=30 * 24 * 3600)


@router.put("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def update_executor_route(
    request: Request, executor_id: str, body: ExecutorUpdateRequest
) -> ExecutorConfigResponse:
    user = require_current_user(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise api_exception(400, "validation_error", "No fields to update")
    policy = await load_executor_policy(request.app.state.session_factory)
    async with request.app.state.session_factory() as session:
        existing = await get_executor_row(session, executor_id, owner_email=user.email)
        if existing is None:
            raise api_exception(404, "not_found", "Executor not found")
        executor_type = str(updates.get("executor_type", existing.executor_type))
        try:
            ensure_executor_type_allowed(executor_type, policy)
            await validate_executor_mcp_scope(
                session,
                owner_email=user.email,
                config=updates.get("config", existing.config or {}),
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        row = await update_executor(session, executor_id, owner_email=user.email, **updates)
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        runtime_affecting = (
            any(key in updates for key in {"config", "enabled_tools", "enabled_tool_groups"})
            and row.executor_type == "websocket"
        )
        if runtime_affecting:
            connected = request.app.state.providers.executor.websocket.get_connection(executor_id)
            desired_version = max(int(getattr(row, "desired_config_version", 0) or 0), 0) + 1
            await update_executor_runtime_state(
                session,
                executor_id,
                desired_config_version=desired_version,
                runtime_state="reconfiguring" if connected is not None else "stale",
            )
        await session.commit()
    if runtime_affecting:
        schedule_executor_reconfigure(request.app, executor_id)
    return _executor_to_response(row)


@router.delete("/api/v1/executors/{executor_id}", status_code=204)
async def delete_executor_route(request: Request, executor_id: str) -> None:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id, owner_email=user.email)
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        if row.is_default:
            raise api_exception(400, "validation_error", "Cannot delete the default executor")
        deleted = await delete_executor(session, executor_id, owner_email=user.email)
        if not deleted:
            raise api_exception(404, "not_found", "Executor not found")
        await session.commit()
