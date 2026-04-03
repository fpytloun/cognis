"""WebSocket endpoint for remote executor connections.

Executors connect to ``WS /api/executor/ws``, authenticate with a JWT
(``aud=["cognis-executor"]``), and send ``executor.ready`` to register
their capabilities.  After authentication the controller may send an
``executor.configure`` message to deliver secrets and additional config.

This endpoint handles its own auth (token-in-first-message pattern,
matching the client WebSocket in ``websocket.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis.logging import get_logger
from cognis.models.tool import ExecutorCapabilities
from cognis.providers.executor.websocket import WebSocketExecutorProvider

_logger = get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 30


async def handle_executor_websocket(
    ws: WebSocket,
    ws_provider: WebSocketExecutorProvider,
    auth_provider: Any,
) -> None:
    """Handle a single executor WebSocket connection.

    Protocol:
    1. Accept the WebSocket.
    2. Wait for the first message: ``executor.ready`` with a JWT token.
    3. Validate the JWT (``aud=["cognis-executor"]``).
    4. Register the connection with ``WebSocketExecutorProvider``.
    5. Optionally send ``executor.configure`` with secrets.
    6. Keep the connection alive until disconnect.
    """
    await ws.accept()

    # ---- Step 1: Read executor.ready with auth token ----
    try:
        first_msg = await asyncio.wait_for(ws.receive_json(), timeout=_AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect):
        await _close_ws(ws, 4408, "Authentication timeout")
        return

    method = first_msg.get("method")
    params = first_msg.get("params", {})
    msg_id = first_msg.get("id")

    if method != "executor.ready":
        await _close_ws(ws, 4400, "First message must be executor.ready")
        return

    executor_id = params.get("executor_id")
    token = params.get("token")
    if not executor_id or not token:
        await _close_ws(ws, 4400, "executor.ready must include executor_id and token")
        return

    # ---- Step 2: Validate JWT ----
    try:
        claims = auth_provider.verify_jwt(token, audience=["cognis-executor"])
    except Exception:
        _logger.warning(
            "executor_ws: auth failed",
            extra={"extra_data": {"executor_id": executor_id}},
        )
        await _send_error(ws, msg_id, -32000, "Authentication failed")
        await _close_ws(ws, 4401, "Invalid token")
        return

    # Verify the token's subject matches the claimed executor_id
    token_sub = claims.get("sub", "")
    if token_sub != executor_id:
        _logger.warning(
            "executor_ws: executor_id mismatch",
            extra={
                "extra_data": {
                    "claimed": executor_id,
                    "token_sub": token_sub,
                }
            },
        )
        await _send_error(ws, msg_id, -32000, "executor_id does not match token subject")
        await _close_ws(ws, 4403, "Executor ID mismatch")
        return

    # ---- Step 3: Parse capabilities and register ----
    raw_caps = params.get("capabilities", {})
    capabilities = ExecutorCapabilities(
        tools=raw_caps.get("tools", []),
        inference=bool(raw_caps.get("inference", False)),
        inference_models=raw_caps.get("inference_models", []),
        inference_type=raw_caps.get("inference_type"),
    )

    conn = ws_provider.register_connection(executor_id, ws, capabilities)

    # Send registration confirmation
    await ws.send_json(
        {
            "jsonrpc": "2.0",
            "result": {"status": "registered"},
            "id": msg_id,
        }
    )

    _logger.info(
        "executor_ws: executor registered",
        extra={
            "extra_data": {
                "executor_id": executor_id,
                "tools_count": len(capabilities.tools),
                "inference": capabilities.inference,
            }
        },
    )

    # ---- Step 4: Keep connection alive ----
    # The WebSocketExecutorConnection's receiver loop handles all
    # subsequent messages.  We just wait for it to finish (disconnect).
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


async def send_executor_configure(
    ws: WebSocket,
    secrets: dict[str, str],
    extra_config: dict[str, Any] | None = None,
) -> None:
    """Send ``executor.configure`` to deliver secrets after auth.

    This is called by the controller after successful registration when
    the executor needs secrets (e.g. for MCP servers).

    Protocol message::

        {
            "jsonrpc": "2.0",
            "method": "executor.configure",
            "params": {"secrets": {...}, "config": {...}},
            "id": "<uuid>"
        }

    The executor responds with ``{"status": "configured"}``.
    """
    import uuid

    request_id = uuid.uuid4().hex
    await ws.send_json(
        {
            "jsonrpc": "2.0",
            "method": "executor.configure",
            "params": {
                "secrets": secrets,
                "config": extra_config or {},
            },
            "id": request_id,
        }
    )
    # We don't wait for the response here — the receiver loop handles it.
    # The executor should process configure before accepting tool.execute.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
    """Close a WebSocket with error code."""
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


async def _send_error(ws: WebSocket, msg_id: Any, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    with contextlib.suppress(Exception):
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "error": {"code": code, "message": message},
                "id": msg_id,
            }
        )
