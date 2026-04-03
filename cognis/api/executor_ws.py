"""WebSocket endpoint for remote executor connections."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis.logging import get_logger
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.store.queries import get_executor_row

_logger = get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 30


async def handle_executor_websocket(
    ws: WebSocket,
    ws_provider: WebSocketExecutorProvider,
    auth_provider: Any,
    session_factory: async_sessionmaker[Any],
) -> None:
    """Handle a remote executor connection and onboard it from controller state."""
    await ws.accept()

    try:
        first_msg = await asyncio.wait_for(ws.receive_json(), timeout=_AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect):
        await _close_ws(ws, 4408, "Authentication timeout")
        return

    method = first_msg.get("method")
    params = first_msg.get("params", {})
    msg_id = first_msg.get("id")
    token = params.get("token")
    if method != "executor.ready" or not token:
        await _close_ws(ws, 4400, "First message must be executor.ready with token")
        return

    try:
        claims = auth_provider.verify_jwt(token, audience=["cognis-executor"])
    except Exception:
        await _send_error(ws, msg_id, -32000, "Authentication failed")
        await _close_ws(ws, 4401, "Invalid token")
        return

    executor_id = str(claims.get("sub", "")).strip()
    if not executor_id:
        await _send_error(ws, msg_id, -32000, "Executor token is missing subject")
        await _close_ws(ws, 4401, "Invalid token")
        return

    async with session_factory() as session:
        row = await get_executor_row(session, executor_id)
    if row is None:
        await _send_error(ws, msg_id, -32004, "Executor not found")
        await _close_ws(ws, 4404, "Executor not found")
        return

    conn = ws_provider.register_connection(
        executor_id,
        ws,
        ExecutorCapabilities(),
        ready=False,
        metadata={
            "labels": row.labels or {},
            "platform": params.get("platform") or {},
            "status": row.status,
        },
    )

    try:
        configure_result = await conn.rpc_call(
            "executor.configure",
            {
                "enabled_tools": row.enabled_tools or [],
                "enabled_tool_groups": row.enabled_tool_groups or [],
                "config": row.config or {},
            },
            timeout=30.0,
        )
    except Exception:
        _logger.warning(
            "executor_ws: executor configuration failed",
            extra={"extra_data": {"executor_id": executor_id}},
            exc_info=True,
        )
        await _send_error(ws, msg_id, -32010, "Executor configuration failed")
        await _close_ws(ws, 1011, "Executor configuration failed")
        ws_provider.unregister_connection(executor_id)
        return

    caps_raw = configure_result.get("capabilities") or {}
    capabilities = ExecutorCapabilities(
        tools=list(caps_raw.get("tools") or []),
        inference=bool(caps_raw.get("inference", False)),
        inference_models=list(caps_raw.get("inference_models") or []),
        inference_type=caps_raw.get("inference_type"),
    )
    ws_provider.mark_ready(
        executor_id,
        capabilities,
        metadata={
            "labels": row.labels or {},
            "platform": params.get("platform") or {},
            "status": row.status,
        },
    )

    await ws.send_json(
        {
            "jsonrpc": "2.0",
            "result": {"status": "registered", "executor_id": executor_id},
            "id": msg_id,
        }
    )

    try:
        await conn.wait_until_closed()
    except Exception:
        _logger.debug(
            "executor_ws: connection ended",
            extra={"extra_data": {"executor_id": executor_id}},
            exc_info=True,
        )
    finally:
        ws_provider.unregister_connection(executor_id)


async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


async def _send_error(ws: WebSocket, msg_id: Any, code: int, message: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "error": {"code": code, "message": message},
                "id": msg_id,
            }
        )
