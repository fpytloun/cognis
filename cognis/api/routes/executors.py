"""Executor configuration CRUD routes."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import or_, update

from cognis.api.common import api_exception, require_admin, require_current_user
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
from cognis.core.executor_token_locks import executor_token_lock
from cognis.ownership import SYSTEM_USER_EMAIL, is_shared_owner_email
from cognis.store.models import ExecutorRow
from cognis.store.queries import (
    create_executor,
    delete_executor,
    get_executor_row,
    list_executors,
    update_executor,
    update_executor_runtime_state,
)

router = APIRouter(tags=["executors"])
LOCAL_EXECUTOR_TYPES = {"in_process", "subprocess"}


def _executor_is_shared(row: Any) -> bool:
    return is_shared_owner_email(getattr(row, "owner_email", None))


def _executor_is_local(row: Any) -> bool:
    return getattr(row, "executor_type", None) in LOCAL_EXECUTOR_TYPES


def _resolve_executor_owner(user: Any, shared: bool) -> str:
    return SYSTEM_USER_EMAIL if shared else user.email


def _enforce_executor_creation_rules(user: Any, *, executor_type: str, shared: bool) -> None:
    if shared and user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can create shared executors")
    if executor_type in LOCAL_EXECUTOR_TYPES and user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can create local executors")


def _require_executor_mutation_access(request: Request, row: Any) -> None:
    user = require_current_user(request)
    if _executor_is_local(row) and user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can manage local executors")
    if _executor_is_shared(row):
        require_admin(request)
        return
    if row.owner_email != user.email:
        raise api_exception(404, "not_found", "Executor not found")


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
        shared=_executor_is_shared(row),
        owner_email=row.owner_email,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _clear_executor_defaults(session: Any, owner_email: str) -> None:
    owner_filter = ExecutorRow.owner_email == owner_email
    if owner_email == SYSTEM_USER_EMAIL:
        owner_filter = or_(owner_filter, ExecutorRow.owner_email.is_(None))
    await session.execute(update(ExecutorRow).where(owner_filter).values(is_default=False))


@router.get("/api/v1/executors", response_model=list[ExecutorConfigResponse])
async def list_executors_route(request: Request) -> list[ExecutorConfigResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_executors(session, owner_email=user.email, include_shared=True)
    return [_executor_to_response(row) for row in rows]


@router.get("/api/v1/executors/{executor_id}", response_model=ExecutorConfigResponse)
async def get_executor_route(request: Request, executor_id: str) -> ExecutorConfigResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_executor_row(
            session, executor_id, owner_email=user.email, include_shared=True
        )
    if row is None:
        raise api_exception(404, "not_found", "Executor not found")
    return _executor_to_response(row)


@router.post("/api/v1/executors", response_model=ExecutorConfigResponse, status_code=201)
async def create_executor_route(
    request: Request, body: ExecutorCreateRequest
) -> ExecutorConfigResponse:
    user = require_current_user(request)
    _enforce_executor_creation_rules(user, executor_type=body.executor_type, shared=body.shared)
    policy = await load_executor_policy(request.app.state.session_factory)
    try:
        ensure_executor_type_allowed(body.executor_type, policy)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    async with request.app.state.session_factory() as session:
        executor_owner = _resolve_executor_owner(user, body.shared)
        try:
            await validate_executor_mcp_scope(
                session,
                owner_email=executor_owner,
                config=body.config or None,
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if body.is_default:
            await _clear_executor_defaults(session, executor_owner)
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
            shared=body.shared,
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
    async with executor_token_lock(executor_id):
        async with request.app.state.session_factory() as session:
            row = await get_executor_row(
                session, executor_id, owner_email=user.email, include_shared=True
            )
            if row is None:
                raise api_exception(404, "not_found", "Executor not found")
            _require_executor_mutation_access(request, row)
            if row.executor_type != "websocket":
                raise api_exception(
                    400,
                    "validation_error",
                    "Only WebSocket executors use persistent tokens",
                )
            result = await session.execute(
                update(ExecutorRow)
                .where(ExecutorRow.executor_id == executor_id)
                .values(token_version=ExecutorRow.token_version + 1)
                .returning(ExecutorRow.token_version)
            )
            token_version = int(result.scalar_one())
            await session.commit()
        connected = request.app.state.providers.executor.websocket.get_connection(executor_id)
        if connected is not None:
            with contextlib.suppress(Exception):
                await connected.close()
    token = request.app.state.providers.auth.sign_executor_token(
        executor_id,
        token_version=token_version,
    )
    return ExecutorTokenResponse(executor_id=executor_id, token=token, expires_in=None)


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
        existing = await get_executor_row(
            session, executor_id, owner_email=user.email, include_shared=True
        )
        if existing is None:
            raise api_exception(404, "not_found", "Executor not found")
        _require_executor_mutation_access(request, existing)
        executor_type = str(updates.get("executor_type", existing.executor_type))
        next_shared = bool(updates.get("shared", _executor_is_shared(existing)))
        executor_owner = _resolve_executor_owner(user, next_shared)
        _enforce_executor_creation_rules(user, executor_type=executor_type, shared=next_shared)
        try:
            ensure_executor_type_allowed(executor_type, policy)
            await validate_executor_mcp_scope(
                session,
                owner_email=executor_owner,
                config=updates.get("config", existing.config or {}),
            )
        except ValueError as exc:
            raise api_exception(400, "validation_error", str(exc)) from exc
        if updates.get("is_default") is True:
            await _clear_executor_defaults(session, executor_owner)
        row = await update_executor(
            session,
            executor_id,
            owner_email=user.email,
            include_shared=True,
            **updates,
        )
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
        row = await get_executor_row(
            session, executor_id, owner_email=user.email, include_shared=True
        )
        if row is None:
            raise api_exception(404, "not_found", "Executor not found")
        _require_executor_mutation_access(request, row)
        if row.is_default:
            raise api_exception(400, "validation_error", "Cannot delete the default executor")
        deleted = await delete_executor(
            session,
            executor_id,
            owner_email=user.email,
            include_shared=True,
        )
        if not deleted:
            raise api_exception(404, "not_found", "Executor not found")
        await session.commit()
