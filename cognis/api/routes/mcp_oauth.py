"""MCP OAuth API routes."""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from cognis.api.common import api_exception, require_current_user
from cognis.core.mcp_oauth import MCPOAuthError, oauth_status_payload
from cognis.models.tool import effective_mcp_auth_config
from cognis.store.queries import (
    get_mcp_oauth_token,
    get_mcp_oauth_token_for_server,
    get_mcp_server,
    mcp_oauth_resource_key,
)

router = APIRouter(tags=["mcp-oauth"])


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
    return oauth_status_payload(row)


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
