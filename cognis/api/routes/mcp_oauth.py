"""MCP OAuth API routes."""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from cognis.api.common import api_exception, require_current_user
from cognis.api.executor_runtime import schedule_executor_reconfigure
from cognis.core.mcp_oauth import MCPOAuthError, oauth_status_payload
from cognis.logging import get_logger
from cognis.models.tool import effective_mcp_auth_config
from cognis.store.queries import (
    get_mcp_oauth_token,
    get_mcp_oauth_token_for_server,
    get_mcp_oauth_transaction,
    get_mcp_server,
    list_pending_mcp_oauth_transactions,
    list_websocket_executors_for_mcp_server,
    mcp_oauth_resource_key,
    update_executor_runtime_state,
)

router = APIRouter(tags=["mcp-oauth"])
logger = get_logger(__name__)


def _service(request: Request) -> Any:
    svc = getattr(request.app.state, "mcp_oauth_service", None)
    if svc is None:
        raise api_exception(503, "unavailable", "MCP OAuth service is not available")
    return svc


@router.post("/api/v1/mcp-servers/{server_id}/oauth/start")
async def start_mcp_oauth(request: Request, server_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    try:
        started = await _service(request).start_authorization(
            user_email=user.email,
            server_id=server_id,
        )
    except MCPOAuthError as exc:
        raise api_exception(400, "mcp_oauth_error", str(exc)) from exc
    return {
        "authorization_url": started.authorization_url,
        "transaction_id": started.transaction_id,
        "expires_at": started.expires_at.isoformat(),
        "issuer": started.issuer,
        "authorization_server": started.authorization_server,
        "scopes": started.scopes,
        "flow": started.flow,
        "verification_uri": started.verification_uri,
        "verification_uri_complete": started.verification_uri_complete,
        "user_code": started.user_code,
        "interval": started.interval,
    }


@router.get("/api/v1/mcp/oauth/callback")
async def mcp_oauth_callback(request: Request, state: str, code: str | None = None) -> HTMLResponse:
    if not code:
        return HTMLResponse("<h1>MCP authorization failed</h1>", status_code=400)
    try:
        transaction_id = await _service(request).complete_callback(state=state, code=code)
    except MCPOAuthError as exc:
        return HTMLResponse(
            f"<h1>MCP authorization failed</h1><p>{escape(str(exc))}</p>",
            status_code=400,
        )
    service = _service(request)
    if getattr(service, "_on_authorization_completed", None) is None:
        await _schedule_mcp_executor_reconfigure(request, transaction_id=transaction_id)
    return HTMLResponse(
        f"<h1>MCP authorization complete</h1><p>Transaction {transaction_id} completed.</p>"
    )


@router.get("/api/v1/mcp-servers/{server_id}/oauth/status")
async def mcp_oauth_status(request: Request, server_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        server = await get_mcp_server(
            session, server_id, owner_email=user.email, include_shared=True
        )
        if server is None:
            raise api_exception(404, "not_found", "MCP server not found")
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = (auth_config.issuer or auth_config.authorization_server or "").rstrip("/")
        resource = auth_config.resource or server.url
        row = None
        if issuer:
            row = await get_mcp_oauth_token(
                session,
                user_email=user.email,
                mcp_server_id=server.server_id,
                issuer=issuer,
                resource=resource,
            )
        else:
            row = await get_mcp_oauth_token_for_server(
                session,
                user_email=user.email,
                mcp_server_id=server.server_id,
            )
    token_payload = None
    if row is not None:
        svc = getattr(request.app.state, "mcp_oauth_service", None)
        try:
            if svc is not None:
                token_payload = svc._decrypt(row.encrypted_payload)
        except Exception:
            logger.warning(
                "mcp oauth: failed to inspect token metadata for status payload",
                extra={"extra_data": {"server_id": server.server_id}},
                exc_info=True,
            )
    payload = oauth_status_payload(row, token_payload)
    pending_authorization = None
    svc = getattr(request.app.state, "mcp_oauth_service", None)
    if svc is not None:
        async with request.app.state.session_factory() as session:
            pending_rows = await list_pending_mcp_oauth_transactions(
                session,
                user_email=user.email,
                mcp_server_id=server.server_id,
            )
            for pending in pending_rows:
                try:
                    pending_payload = svc._decrypt(pending.encrypted_payload)
                except Exception:
                    continue
                if pending_payload.get("flow") != "device_code":
                    continue
                pending_authorization = {
                    "flow": "device_code",
                    "transaction_id": pending.transaction_id,
                    "verification_uri": pending_payload.get("verification_uri"),
                    "verification_uri_complete": pending_payload.get("verification_uri_complete"),
                    "user_code": pending_payload.get("user_code"),
                    "expires_at": pending.expires_at.isoformat(),
                    "interval": pending_payload.get("interval"),
                }
                break
    if pending_authorization is not None:
        payload["pending_authorization"] = pending_authorization
    return payload


@router.post("/api/v1/mcp-servers/{server_id}/oauth/disconnect")
async def disconnect_mcp_oauth(request: Request, server_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        server = await get_mcp_server(
            session, server_id, owner_email=user.email, include_shared=True
        )
        if server is None:
            raise api_exception(404, "not_found", "MCP server not found")
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = (auth_config.issuer or auth_config.authorization_server or "").rstrip("/")
        row = None
        if issuer:
            row = await get_mcp_oauth_token(
                session,
                user_email=user.email,
                mcp_server_id=server.server_id,
                issuer=issuer,
                resource=auth_config.resource or server.url,
            )
        else:
            row = await get_mcp_oauth_token_for_server(
                session,
                user_email=user.email,
                mcp_server_id=server.server_id,
            )
        if row is not None:
            row.status = "revoked"
            row.resource_key = row.resource_key or mcp_oauth_resource_key(row.resource)
            await session.commit()
    return {"status": "disconnected"}


async def _schedule_mcp_executor_reconfigure(request: Request, *, transaction_id: str) -> None:
    await schedule_mcp_executor_reconfigure_for_app(request.app, transaction_id=transaction_id)


async def schedule_mcp_executor_reconfigure_for_app(app: Any, *, transaction_id: str) -> None:
    async with app.state.session_factory() as session:
        transaction = await get_mcp_oauth_transaction(session, transaction_id)
        if transaction is None:
            return
        executors = await list_websocket_executors_for_mcp_server(
            session, transaction.mcp_server_id
        )
        scheduled_ids: list[str] = []
        ws_provider = app.state.providers.executor.websocket
        for row in executors:
            connected = ws_provider.get_connection(row.executor_id)
            desired_version = max(int(getattr(row, "desired_config_version", 0) or 0), 0) + 1
            await update_executor_runtime_state(
                session,
                row.executor_id,
                desired_config_version=desired_version,
                runtime_state="reconfiguring" if connected is not None else "stale",
            )
            scheduled_ids.append(row.executor_id)
        await session.commit()
    for executor_id in scheduled_ids:
        schedule_executor_reconfigure(app, executor_id)
    if scheduled_ids:
        logger.info(
            "mcp oauth: scheduled executor reconfigure after authorization",
            extra={
                "extra_data": {
                    "transaction_id": transaction_id,
                    "executor_ids": scheduled_ids,
                }
            },
        )
