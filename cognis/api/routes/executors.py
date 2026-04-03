"""Executor configuration CRUD routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_admin, require_current_user
from cognis.api.models import (
    ExecutorConfigResponse,
    ExecutorCreateRequest,
    ExecutorTokenResponse,
    ExecutorUpdateRequest,
)
from cognis.store.queries import (
    create_executor,
    delete_executor,
    get_executor_row,
    list_executors,
    update_executor,
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
        is_default=row.is_default,
        owner_email=row.owner_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/v1/executors", response_model=list[ExecutorConfigResponse])
async def list_executors_route(request: Request) -> list[ExecutorConfigResponse]:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_executors(session)
    return [_executor_to_response(row) for row in rows]


@router.get("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def get_executor_route(request: Request, executor_id: str) -> ExecutorConfigResponse:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id)
    if row is None:
        raise api_exception(404, "not_found", "Executor not found")
    return _executor_to_response(row)


@router.post("/api/v1/executors", response_model=ExecutorConfigResponse, status_code=201)
async def create_executor_route(
    request: Request, body: ExecutorCreateRequest
) -> ExecutorConfigResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
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
        await session.commit()
    return _executor_to_response(row)


@router.post("/api/v1/executors/{executor_id}/token", response_model=ExecutorTokenResponse)
async def generate_executor_token_route(
    request: Request,
    executor_id: str,
) -> ExecutorTokenResponse:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id)
    if row is None:
        raise api_exception(404, "not_found", "Executor not found")
    token = request.app.state.providers.auth.sign_executor_token(executor_id)
    return ExecutorTokenResponse(executor_id=executor_id, token=token, expires_in=30 * 24 * 3600)


@router.put("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def update_executor_route(
    request: Request, executor_id: str, body: ExecutorUpdateRequest
) -> ExecutorConfigResponse:
    require_current_user(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise api_exception(400, "validation_error", "No fields to update")
    async with request.app.state.session_factory() as session:
        row = await update_executor(session, executor_id, **updates)
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        await session.commit()
    return _executor_to_response(row)


@router.delete("/api/v1/executors/{executor_id}", status_code=204)
async def delete_executor_route(request: Request, executor_id: str) -> None:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(session, executor_id)
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        if row.is_default:
            raise api_exception(400, "validation_error", "Cannot delete the default executor")
        deleted = await delete_executor(session, executor_id)
        if not deleted:
            raise api_exception(404, "not_found", "Executor not found")
        await session.commit()
