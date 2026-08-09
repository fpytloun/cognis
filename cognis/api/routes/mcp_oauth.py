"""MCP OAuth API routes."""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from cognis.api.common import api_exception, require_current_user
from cognis.api.mcp_reconfigure import schedule_mcp_server_executor_reconfigure_for_app
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
        "resource": started.resource,
        "flow": started.flow,
        "verification_uri": started.verification_uri,
        "verification_uri_complete": started.verification_uri_complete,
        "user_code": started.user_code,
        "interval": started.interval,
        "callback_mode": started.callback_mode,
        "oauth_executor_id": started.oauth_executor_id,
        "oauth_executor_name": started.oauth_executor_name,
        "redirect_uri": started.redirect_uri,
        "instructions": started.instructions,
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
    payload = await _mcp_oauth_status_payload_for_user(
        request.app,
        user_email=user.email,
        server_id=server_id,
    )
    if payload is None:
        raise api_exception(404, "not_found", "MCP server not found")
    return payload


async def _mcp_oauth_status_payload_for_user(
    app: Any,
    *,
    user_email: str,
    server_id: str,
) -> dict[str, Any] | None:
    async with app.state.session_factory() as session:
        server = await get_mcp_server(
            session, server_id, owner_email=user_email, include_shared=True
        )
        if server is None:
            return None
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = (auth_config.issuer or auth_config.authorization_server or "").rstrip("/")
        resource = auth_config.resource or server.url
        row = None
        if issuer:
            row = await get_mcp_oauth_token(
                session,
                user_email=user_email,
                mcp_server_id=server.server_id,
                issuer=issuer,
                resource=resource,
            )
        else:
            row = await get_mcp_oauth_token_for_server(
                session,
                user_email=user_email,
                mcp_server_id=server.server_id,
            )
    token_payload = None
    if row is not None:
        svc = getattr(app.state, "mcp_oauth_service", None)
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
    async with app.state.session_factory() as session:
        executor_rows = await list_websocket_executors_for_mcp_server(
            session,
            server.server_id,
        )
    executors: list[dict[str, Any]] = []
    for executor in executor_rows:
        runtime_metadata = (
            executor.runtime_metadata if isinstance(executor.runtime_metadata, dict) else {}
        )
        server_status = next(
            (
                item
                for item in runtime_metadata.get("mcp_servers", [])
                if isinstance(item, dict) and item.get("server_id") == server.server_id
            ),
            None,
        )
        converged = int(executor.desired_config_version or 0) == int(
            executor.applied_config_version or 0
        )
        websocket_provider = getattr(
            getattr(getattr(app.state, "providers", None), "executor", None),
            "websocket",
            None,
        )
        connection = (
            websocket_provider.get_connection(executor.executor_id)
            if websocket_provider is not None
            else None
        )
        ready = bool(
            converged
            and executor.status == "active"
            and executor.runtime_state in {"active", "degraded"}
            and connection is not None
            and isinstance(server_status, dict)
            and server_status.get("status") == "ready"
        )
        executors.append(
            {
                "executor_id": executor.executor_id,
                "runtime_state": executor.runtime_state,
                "desired_config_version": int(executor.desired_config_version or 0),
                "applied_config_version": int(executor.applied_config_version or 0),
                "converged": converged,
                "mcp_status": server_status.get("status")
                if isinstance(server_status, dict)
                else None,
                "ready": ready,
            }
        )
    runtime_connected = bool(
        payload.get("connected")
        and (any(item["ready"] for item in executors) if executors else True)
    )
    payload["runtime_connected"] = runtime_connected
    payload["runtime"] = {
        "state": (
            "connected" if runtime_connected else "not_assigned" if not executors else "degraded"
        ),
        "executors": executors,
    }
    pending_authorization = None
    svc = getattr(app.state, "mcp_oauth_service", None)
    if svc is not None:
        async with app.state.session_factory() as session:
            pending_rows = await list_pending_mcp_oauth_transactions(
                session,
                user_email=user_email,
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


async def _emit_mcp_oauth_status_changed(
    app: Any,
    *,
    user_email: str,
    server_id: str,
) -> None:
    payload = await _mcp_oauth_status_payload_for_user(
        app,
        user_email=user_email,
        server_id=server_id,
    )
    if payload is None:
        return
    websocket_manager = getattr(app.state, "websocket_manager", None)
    if websocket_manager is None:
        return
    await websocket_manager.send_to_user(
        user_email,
        {
            "type": "mcp_oauth_status_changed",
            "server_id": server_id,
            "status": payload,
        },
    )


async def emit_mcp_oauth_status_changed_for_app(
    app: Any,
    *,
    user_email: str,
    server_id: str,
) -> None:
    """Emit the additive OAuth status event from controller lifecycle callbacks."""

    await _emit_mcp_oauth_status_changed(
        app,
        user_email=user_email,
        server_id=server_id,
    )


@router.post("/api/v1/mcp-servers/{server_id}/oauth/disconnect")
async def disconnect_mcp_oauth(request: Request, server_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    return await disconnect_mcp_oauth_for_user(
        request.app, user_email=user.email, server_id=server_id
    )


async def disconnect_mcp_oauth_for_user(
    app: Any,
    *,
    user_email: str,
    server_id: str,
) -> dict[str, Any]:
    """Disconnect one user's MCP OAuth token and publish runtime status changes."""
    should_reconfigure = False
    async with app.state.session_factory() as session:
        server = await get_mcp_server(
            session, server_id, owner_email=user_email, include_shared=True
        )
        if server is None:
            raise api_exception(404, "not_found", "MCP server not found")
        auth_config = effective_mcp_auth_config(server.auth_config, server.headers)
        issuer = (auth_config.issuer or auth_config.authorization_server or "").rstrip("/")
        row = None
        if issuer:
            row = await get_mcp_oauth_token(
                session,
                user_email=user_email,
                mcp_server_id=server.server_id,
                issuer=issuer,
                resource=auth_config.resource or server.url,
            )
        else:
            row = await get_mcp_oauth_token_for_server(
                session,
                user_email=user_email,
                mcp_server_id=server.server_id,
            )
        if row is not None:
            row.status = "revoked"
            row.resource_key = row.resource_key or mcp_oauth_resource_key(row.resource)
            await session.commit()
            should_reconfigure = True
    if should_reconfigure:
        await schedule_mcp_server_executor_reconfigure_for_app(
            app,
            server_id=server_id,
            reason="mcp_oauth_disconnect",
        )
    await _emit_mcp_oauth_status_changed(app=app, user_email=user_email, server_id=server_id)
    return {"status": "disconnected"}


async def _schedule_mcp_executor_reconfigure(request: Request, *, transaction_id: str) -> None:
    await schedule_mcp_executor_reconfigure_for_app(request.app, transaction_id=transaction_id)


async def schedule_mcp_executor_reconfigure_for_app(
    app: Any,
    *,
    transaction_id: str,
    admission_guard: Any | None = None,
    terminal_cleanup: bool = False,
) -> None:
    async with app.state.session_factory() as session:
        transaction = await get_mcp_oauth_transaction(session, transaction_id)
        if transaction is None:
            return
        user_email = transaction.user_email
        server_id = transaction.mcp_server_id
    schedule_kwargs: dict[str, Any] = {
        "server_id": server_id,
        "reason": "mcp_oauth_authorization",
        "log_context": {"transaction_id": transaction_id},
    }
    if admission_guard is not None:
        schedule_kwargs["admission_guard"] = admission_guard
    if terminal_cleanup:
        schedule_kwargs["terminal_cleanup_transaction_id"] = transaction_id
    scheduled = await schedule_mcp_server_executor_reconfigure_for_app(app, **schedule_kwargs)
    if scheduled is None:
        return
    await _emit_mcp_oauth_status_changed(app, user_email=user_email, server_id=server_id)
