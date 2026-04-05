"""WebSocket endpoint for remote executor connections."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis.core.executor_policy import is_executor_type_allowed, load_executor_policy
from cognis.logging import get_logger
from cognis.models.tool import MCP_SERVER_IDS_KEY, ExecutorCapabilities, MCPServerConfig
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.store.queries import get_executor_row, get_mcp_server, update_executor_runtime_state

_logger = get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 30


async def handle_executor_websocket(
    ws: WebSocket,
    ws_provider: WebSocketExecutorProvider,
    providers: Any,
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
        claims = providers.auth.verify_jwt(token, audience=["cognis-executor"])
    except Exception:
        await _send_error(ws, msg_id, -32000, "Authentication failed")
        await _close_ws(ws, 4401, "Invalid token")
        return

    executor_id = str(claims.get("sub", "")).strip()
    if not executor_id:
        await _send_error(ws, msg_id, -32000, "Executor token is missing subject")
        await _close_ws(ws, 4401, "Invalid token")
        return

    policy = await load_executor_policy(session_factory)
    async with session_factory() as session:
        row = await get_executor_row(session, executor_id)
    if row is None:
        await _send_error(ws, msg_id, -32004, "Executor not found")
        await _close_ws(ws, 4404, "Executor not found")
        return

    if row.status != "active":
        await _send_error(ws, msg_id, -32005, "Executor is inactive")
        await _close_ws(ws, 4403, "Executor inactive")
        return
    if not is_executor_type_allowed(row.executor_type, policy):
        await _send_error(ws, msg_id, -32006, "Executor type is disabled by policy")
        await _close_ws(ws, 4403, "Executor type disabled")
        return

    conn = ws_provider.register_connection(
        executor_id,
        ws,
        ExecutorCapabilities(),
        ready=False,
        metadata=_executor_connection_metadata(
            labels=row.labels or {},
            environment=params.get("environment"),
            platform=params.get("platform") or {},
            status=row.status,
        ),
    )

    # Acknowledge executor.ready before sending executor.configure.
    # The runner waits for this response before entering the normal
    # message loop that can process controller-initiated RPC requests.
    await ws.send_json(
        {
            "jsonrpc": "2.0",
            "result": {"status": "registered", "executor_id": executor_id},
            "id": msg_id,
        }
    )

    try:
        desired_version = row.desired_config_version + 1
        mcp_servers, scoped_secrets = await _resolve_executor_mcp_payload(row, providers)

        # Resolve web backend config so the executor can register web tools
        from cognis.api.runtime_support import _resolve_web_config

        web_config = await _resolve_web_config(providers, row.owner_email)
        # Merge web API keys into scoped_secrets so the executor has them
        scoped_secrets.update(web_config.get("web_secrets", {}))

        async with session_factory() as session:
            await update_executor_runtime_state(
                session,
                executor_id,
                desired_config_version=desired_version,
                runtime_state="reconfiguring",
            )
            await session.commit()
        configure_result = await conn.rpc_call(
            "executor.configure",
            {
                "config_version": desired_version,
                "enabled_tools": row.enabled_tools or [],
                "enabled_tool_groups": row.enabled_tool_groups or [],
                "config": row.config or {},
                "mcp_servers": [server.model_dump(mode="json") for server in mcp_servers],
                "secrets": scoped_secrets,
                "web_config": {
                    "web_backend": web_config.get("web_backend", "direct"),
                    "web_available_backends": web_config.get("web_available_backends", ["direct"]),
                },
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
        async with session_factory() as session:
            await update_executor_runtime_state(session, executor_id, runtime_state="blocked")
            await session.commit()
        return

    caps_raw = configure_result.get("capabilities") or {}
    applied_version = int(configure_result.get("applied_version") or desired_version)
    observed_tools = list(configure_result.get("observed_tools") or [])
    capabilities = ExecutorCapabilities(
        tools=list(caps_raw.get("tools") or []),
        inference=bool(caps_raw.get("inference", False)),
        inference_models=list(caps_raw.get("inference_models") or []),
        inference_type=caps_raw.get("inference_type"),
        channels=bool(caps_raw.get("channels", False)),
    )
    ws_provider.mark_ready(
        executor_id,
        capabilities,
        metadata=_executor_connection_metadata(
            labels=row.labels or {},
            environment=(
                configure_result.get("environment")
                if configure_result.get("environment") is not None
                else params.get("environment")
            ),
            platform=params.get("platform") or {},
            status=row.status,
        ),
    )

    async with session_factory() as session:
        await update_executor_runtime_state(
            session,
            executor_id,
            applied_config_version=applied_version,
            observed_tools=observed_tools,
            last_observed_at=datetime.now(UTC),
            runtime_state="active",
        )
        await session.commit()

    # Start any channel accounts assigned to this executor
    channel_manager = getattr(ws.app.state, "channel_manager", None)
    if channel_manager is not None:
        try:
            await channel_manager.start_executor_channels(executor_id, conn)
        except Exception:
            _logger.warning(
                "executor_ws: failed to start executor channels",
                extra={"extra_data": {"executor_id": executor_id}},
                exc_info=True,
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
        # Clean up executor-hosted channel adapters before unregistering
        if channel_manager is not None:
            try:
                await channel_manager.stop_executor_channels(executor_id)
            except Exception:
                _logger.warning(
                    "executor_ws: failed to stop executor channels on disconnect",
                    extra={"extra_data": {"executor_id": executor_id}},
                    exc_info=True,
                )
        ws_provider.unregister_connection(executor_id)
        async with session_factory() as session:
            await update_executor_runtime_state(session, executor_id, runtime_state="offline")
            await session.commit()


async def _resolve_executor_mcp_payload(
    row: Any, providers: Any
) -> tuple[list[MCPServerConfig], dict[str, str]]:
    server_ids = (row.config or {}).get(MCP_SERVER_IDS_KEY, [])
    if not isinstance(server_ids, list) or not server_ids:
        return [], {}

    servers: list[MCPServerConfig] = []
    secret_names: set[str] = set()
    async with providers._session_factory() as session:
        for server_id in server_ids:
            mcp_row = await get_mcp_server(session, str(server_id), owner_email=row.owner_email)
            if mcp_row is None or mcp_row.status != "active":
                continue
            servers.append(
                MCPServerConfig(
                    name=mcp_row.name,
                    transport=mcp_row.transport,
                    command=mcp_row.command,
                    url=mcp_row.url,
                    args=mcp_row.args or [],
                    env=mcp_row.env or {},
                    timeout_seconds=mcp_row.timeout_seconds,
                    server_id=mcp_row.server_id,
                )
            )
            for value in (mcp_row.env or {}).values():
                if isinstance(value, str) and value.startswith("$secret:"):
                    secret_names.add(value[len("$secret:") :])

    secrets: dict[str, str] = {}
    for name in secret_names:
        try:
            secrets[name] = await providers.secrets.get_secret(name, row.owner_email)
        except Exception:
            continue
    return servers, secrets


async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


def _executor_connection_metadata(
    *,
    labels: dict[str, Any],
    environment: Any,
    platform: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "labels": labels,
        "platform": platform,
        "status": status,
    }
    if isinstance(environment, dict):
        metadata["environment"] = environment
    return metadata


async def _send_error(ws: WebSocket, msg_id: Any, code: int, message: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(
            {
                "jsonrpc": "2.0",
                "error": {"code": code, "message": message},
                "id": msg_id,
            }
        )
