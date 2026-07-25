"""WebSocket endpoint for remote executor connections."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.websockets import WebSocket, WebSocketDisconnect

from cognis.api.executor_runtime import (
    persist_executor_resource_snapshot,
    reconcile_executor,
)
from cognis.core.executor_policy import is_executor_type_allowed, load_executor_policy
from cognis.core.executor_token_locks import executor_token_lock
from cognis.core.mcp_oauth import MCPOAuthError, oauth_required_mcp_status
from cognis.logging import get_logger
from cognis.models.executor_resources import (
    ExecutorResourceSnapshot,
    normalize_executor_resource_snapshot,
)
from cognis.models.tool import (
    MCP_SERVER_IDS_KEY,
    ExecutorCapabilities,
    MCPAuthConfig,
    MCPServerConfig,
    effective_mcp_auth_config,
)
from cognis.ownership import is_shared_owner_email
from cognis.providers.executor.websocket import WebSocketExecutorProvider
from cognis.store.queries import (
    get_executor_row,
    get_mcp_server,
    get_setting_value,
    update_executor_runtime_state,
)
from cognis.tools.mcp import invalid_mcp_config_reason

_logger = get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 30


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _runtime_authorization_challenge(row: Any, server_id: str) -> dict[str, str] | None:
    runtime_metadata = getattr(row, "runtime_metadata", None)
    if not isinstance(runtime_metadata, dict):
        return None
    server_statuses = runtime_metadata.get("mcp_servers")
    if not isinstance(server_statuses, list):
        return None
    for status in reversed(server_statuses):
        if not isinstance(status, dict) or status.get("server_id") != server_id:
            continue
        challenge = status.get("authorization_challenge")
        if not isinstance(challenge, dict):
            continue
        safe_challenge = {
            key: value
            for key, value in challenge.items()
            if key in {"scope", "resource_metadata", "authorization_uri", "issuer", "error"}
            and isinstance(value, str)
        }
        return safe_challenge or None
    return None


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
        claims = providers.auth.verify_executor_token(token)
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
    async with executor_token_lock(executor_id):
        async with session_factory() as session:
            row = await get_executor_row(session, executor_id)
        if row is None:
            await _send_error(ws, msg_id, -32004, "Executor not found")
            await _close_ws(ws, 4404, "Executor not found")
            return

        if row.executor_type != "websocket" and _executor_token_expired(claims):
            await _send_error(ws, msg_id, -32000, "Authentication failed")
            await _close_ws(ws, 4401, "Invalid token")
            return

        token_version = _executor_token_version(claims)
        expected_token_version = int(getattr(row, "token_version", 0) or 0)
        if token_version != expected_token_version:
            await _send_error(ws, msg_id, -32000, "Executor token has been revoked")
            await _close_ws(ws, 4401, "Invalid token")
            return

        if row.status != "active":
            await _send_error(ws, msg_id, -32005, "Executor is inactive")
            await _close_ws(ws, 4403, "Executor inactive")
            return
        if not is_executor_type_allowed(row.executor_type, policy):
            await _send_error(ws, msg_id, -32006, "Executor type is disabled by policy")
            await _close_ws(ws, 4403, "Executor type disabled")
            return

        _logger.info(
            "executor_ws: registering executor %s (type=%s, owner=%s)",
            executor_id,
            row.executor_type,
            row.owner_email,
        )
        ready_snapshot = normalize_executor_resource_snapshot(params.get("resource_snapshot"))
        ready_received_at = datetime.now(UTC)
        ready_runtime_metadata = _ready_runtime_metadata(
            row,
            environment=params.get("environment"),
            platform=params.get("platform"),
            resource_snapshot=ready_snapshot,
            received_at=ready_received_at,
        )
        conn = ws_provider.register_connection(
            executor_id,
            ws,
            ExecutorCapabilities(),
            ready=False,
            metadata=_executor_connection_metadata(
                labels=row.labels or {},
                environment=ready_runtime_metadata.get("environment"),
                platform=ready_runtime_metadata.get("platform") or {},
                resource_snapshot=ready_snapshot,
                status=row.status,
                owner_email=row.owner_email,
            ),
        )

        async def _resource_snapshot_received(
            callback_executor_id: str,
            payload: dict[str, Any],
        ) -> None:
            try:
                await persist_executor_resource_snapshot(
                    ws.app,
                    callback_executor_id,
                    payload,
                    connection=conn,
                )
            except Exception:
                _logger.debug(
                    "executor_ws: failed to persist resource snapshot",
                    extra={"extra_data": {"executor_id": callback_executor_id}},
                    exc_info=True,
                )

        conn.register_resource_snapshot_callback(_resource_snapshot_received)

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

    # Reset DB runtime_state so reconcile_executor() does not hit the fast
    # path that skips sending executor.configure.  After a controller crash
    # the DB may still show "active" with applied == desired, but the
    # freshly-reconnected executor has _configured=False and needs a full
    # configure handshake.
    async with session_factory() as session:
        await update_executor_runtime_state(
            session,
            executor_id,
            runtime_state="offline",
            runtime_metadata=ready_runtime_metadata,
            last_observed_at=ready_received_at if ready_snapshot is not None else None,
        )
        await session.commit()

    _logger.info("executor_ws: executor %s registered, starting reconcile", executor_id)
    try:
        configure_ok = await reconcile_executor(ws.app, executor_id, connection=conn)
    except Exception:
        _logger.warning(
            "executor_ws: executor %s configuration failed",
            executor_id,
            exc_info=True,
        )
        configure_ok = False
        async with session_factory() as session:
            current = await get_executor_row(session, executor_id)
            if current is not None and current.runtime_state == "reconfiguring":
                await update_executor_runtime_state(session, executor_id, runtime_state="blocked")
                await session.commit()

    async with session_factory() as session:
        row = await get_executor_row(session, executor_id)
    runtime_state = getattr(row, "runtime_state", "offline") if row is not None else "offline"
    _logger.info(
        "executor_ws: executor %s post-configure state: %s (configure_ok=%s)",
        executor_id,
        runtime_state,
        configure_ok,
    )
    local_model_reconciler = getattr(ws.app.state, "local_model_reconciler", None)
    if local_model_reconciler is not None and configure_ok:
        local_model_runtime_manager = getattr(
            ws.app.state,
            "local_model_runtime_manager",
            None,
        )
        if local_model_runtime_manager is not None:
            await local_model_runtime_manager.executor_connected(executor_id)
        local_model_reconciler.trigger(executor_id=executor_id)
    if configure_ok and runtime_state in {"active", "degraded"}:
        ws_provider.schedule_browser_terminal_flush(executor_id)

    # Start any channel accounts assigned to this executor
    channel_manager = getattr(ws.app.state, "channel_manager", None)
    if channel_manager is not None and configure_ok and runtime_state in {"active", "degraded"}:
        try:
            await channel_manager.start_executor_channels(executor_id, conn)
        except Exception:
            _logger.warning(
                "executor_ws: failed to start executor channels",
                extra={"extra_data": {"executor_id": executor_id}},
                exc_info=True,
            )

    _logger.info("executor_ws: executor %s ready, entering connection loop", executor_id)
    try:
        await conn.wait_until_closed()
    except Exception:
        _logger.debug(
            "executor_ws: executor %s connection ended",
            executor_id,
            exc_info=True,
        )
    finally:
        is_current = ws_provider.owns_connection(executor_id, conn)
        _logger.info(
            "executor_ws: executor %s disconnected (is_current=%s)",
            executor_id,
            is_current,
            extra={
                "extra_data": {
                    "executor_id": executor_id,
                    "is_current": is_current,
                    **conn.close_metadata,
                }
            },
        )
        # Clean up executor-hosted channel adapters before unregistering
        if channel_manager is not None and is_current:
            try:
                await channel_manager.stop_executor_channels(executor_id)
            except Exception:
                _logger.warning(
                    "executor_ws: failed to stop executor channels on disconnect",
                    extra={"extra_data": {"executor_id": executor_id}},
                    exc_info=True,
                )
        local_model_runtime_manager = getattr(
            ws.app.state,
            "local_model_runtime_manager",
            None,
        )
        if local_model_runtime_manager is not None and is_current:
            with contextlib.suppress(Exception):
                await local_model_runtime_manager.executor_disconnected(executor_id)
        ws_provider.unregister_connection(executor_id, conn)
        if is_current:
            async with session_factory() as session:
                current = await get_executor_row(session, executor_id)
                next_state = "offline"
                if (
                    current is not None
                    and current.desired_config_version != current.applied_config_version
                ):
                    next_state = "stale"
                await update_executor_runtime_state(session, executor_id, runtime_state=next_state)
                await session.commit()


async def _resolve_executor_mcp_payload(
    row: Any, providers: Any
) -> tuple[list[MCPServerConfig], dict[str, str], dict[str, Any]]:
    server_ids = (row.config or {}).get(MCP_SERVER_IDS_KEY, [])
    if not isinstance(server_ids, list) or not server_ids:
        return [], {}, {}

    servers: list[MCPServerConfig] = []
    skipped_statuses: list[dict[str, Any]] = []
    warnings: list[str] = []
    secret_names: set[str] = set()
    async with providers._session_factory() as session:
        tool_timeout_raw = await get_setting_value(session, "mcp.tool_timeout_seconds", 300)
        connect_timeout_raw = await get_setting_value(session, "mcp.connect_timeout_seconds", 15)
        tool_timeout = _coerce_positive_int(tool_timeout_raw, 300)
        connect_timeout = _coerce_positive_int(connect_timeout_raw, 15)
        for server_id in server_ids:
            mcp_row = await get_mcp_server(
                session,
                str(server_id),
                owner_email=row.owner_email,
                include_shared=True,
            )
            if mcp_row is None or mcp_row.status != "active":
                continue
            invalid_reason = invalid_mcp_config_reason(
                transport=mcp_row.transport,
                command=mcp_row.command,
                url=mcp_row.url,
                env=mcp_row.env,
                headers=mcp_row.headers,
                auth_config=mcp_row.auth_config,
            )
            if invalid_reason is not None:
                _logger.warning(
                    "Skipping invalid MCP server config",
                    extra={"extra_data": {"server_id": server_id, "reason": invalid_reason}},
                )
                continue
            headers = mcp_row.headers or {}
            auth_config = effective_mcp_auth_config(mcp_row.auth_config, headers)
            oauth_service = getattr(providers, "mcp_oauth_service", None) or getattr(
                providers, "_mcp_oauth_service", None
            )
            if auth_config.type == "oauth2" and (oauth_service is None or not row.owner_email):
                warning = (
                    f"MCP server {mcp_row.name} requires OAuth authorization, "
                    "but no OAuth context is available."
                )
                warnings.append(warning)
                skipped_statuses.append(
                    oauth_required_mcp_status(
                        server_id=server_id,
                        server_name=mcp_row.name,
                        reason="oauth_context_unavailable",
                    )
                )
                _logger.warning(
                    "Skipping OAuth MCP server without OAuth context",
                    extra={
                        "extra_data": {
                            "server_id": server_id,
                            "server_name": mcp_row.name,
                            "has_oauth_service": oauth_service is not None,
                            "has_owner_email": bool(row.owner_email),
                        }
                    },
                )
                continue
            if auth_config.type == "oauth2" and oauth_service is not None and row.owner_email:
                try:
                    result = await oauth_service.inject_authorization_header(
                        user_email=row.owner_email,
                        server=mcp_row,
                        headers={k: v for k, v in headers.items() if k.lower() != "authorization"},
                        authorization_challenge=_runtime_authorization_challenge(
                            row,
                            str(server_id),
                        ),
                    )
                except MCPOAuthError as exc:
                    warning = (
                        f"MCP server {mcp_row.name} requires OAuth authorization, "
                        "but authorization metadata could not be resolved."
                    )
                    warnings.append(warning)
                    skipped_statuses.append(
                        oauth_required_mcp_status(
                            server_id=server_id,
                            server_name=mcp_row.name,
                            reason=str(exc)[:240],
                        )
                    )
                    _logger.warning(
                        "Skipping OAuth MCP server with unresolved authorization metadata",
                        extra={
                            "extra_data": {
                                "server_id": server_id,
                                "server_name": mcp_row.name,
                                "reason": str(exc)[:240],
                            }
                        },
                    )
                    continue
                if result.authorization_required:
                    warning = (
                        f"MCP server {mcp_row.name} requires OAuth authorization before "
                        "tools can be discovered."
                    )
                    warnings.append(warning)
                    skipped_statuses.append(
                        oauth_required_mcp_status(
                            server_id=server_id,
                            server_name=mcp_row.name,
                            reason=result.reason,
                            transaction_id=result.transaction_id,
                            authorization_url=result.authorization_url,
                            flow=result.flow,
                            verification_uri=result.verification_uri,
                            verification_uri_complete=result.verification_uri_complete,
                            user_code=result.user_code,
                            callback_mode=result.callback_mode,
                            oauth_executor_id=result.oauth_executor_id,
                            oauth_executor_name=result.oauth_executor_name,
                            redirect_uri=result.redirect_uri,
                            instructions=result.instructions,
                            scopes=result.scopes,
                            resource=result.resource,
                        )
                    )
                    _logger.warning(
                        "OAuth MCP server requires authorization",
                        extra={
                            "extra_data": {
                                "server_id": server_id,
                                "reason": result.reason,
                                "transaction_id": result.transaction_id,
                            }
                        },
                    )
                    continue
                headers = result.headers
                auth_config = MCPAuthConfig(type="static_headers")
            servers.append(
                MCPServerConfig(
                    name=mcp_row.name,
                    transport=mcp_row.transport,
                    command=mcp_row.command,
                    url=mcp_row.url,
                    args=mcp_row.args or [],
                    env=mcp_row.env or {},
                    headers=headers,
                    auth_config=auth_config,
                    timeout_seconds=max(int(mcp_row.timeout_seconds or 0), tool_timeout),
                    connect_timeout_seconds=connect_timeout,
                    server_id=mcp_row.server_id,
                )
            )
            for value in [*(mcp_row.env or {}).values(), *(mcp_row.headers or {}).values()]:
                if isinstance(value, str) and value.startswith("$secret:"):
                    secret_names.add(value[len("$secret:") :])

    secrets: dict[str, str] = {}
    for name in secret_names:
        try:
            secrets[name] = await providers.secrets.get_secret(name, row.owner_email)
        except Exception:
            continue
    metadata: dict[str, Any] = {}
    if skipped_statuses:
        metadata["mcp_servers"] = skipped_statuses
    if warnings:
        metadata["warnings"] = warnings
    return servers, secrets, metadata


async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


def _executor_token_version(claims: dict[str, Any]) -> int:
    try:
        return int(claims.get("etv", 0) or 0)
    except (TypeError, ValueError):
        return -1


def _executor_token_expired(claims: dict[str, Any]) -> bool:
    try:
        exp = int(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return True
    return exp <= int(datetime.now(UTC).timestamp())


def _executor_connection_metadata(
    *,
    labels: dict[str, Any],
    environment: Any,
    platform: dict[str, Any],
    resource_snapshot: ExecutorResourceSnapshot | None,
    status: str,
    owner_email: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "labels": labels,
        "platform": platform,
        "status": status,
        "owner_email": owner_email,
        "shared": is_shared_owner_email(owner_email),
    }
    if isinstance(environment, dict):
        metadata["environment"] = environment
    if resource_snapshot is not None:
        metadata["resource_snapshot"] = resource_snapshot.model_dump(
            mode="json",
            exclude={"freshness"},
        )
    return metadata


def _ready_runtime_metadata(
    row: Any,
    *,
    environment: Any,
    platform: Any,
    resource_snapshot: ExecutorResourceSnapshot | None,
    received_at: datetime,
) -> dict[str, Any]:
    """Merge authenticated ready metadata without losing prior runtime state."""

    metadata = dict(getattr(row, "runtime_metadata", None) or {})
    if isinstance(environment, dict):
        metadata["environment"] = {
            key: value[:1024]
            for key, value in environment.items()
            if key in {"user", "home", "cwd", "hostname", "source", "observed_at"}
            and isinstance(value, str)
        }
    if isinstance(platform, dict):
        metadata["platform"] = {
            key: value[:128]
            for key, value in platform.items()
            if key in {"os", "arch", "python"} and isinstance(value, str)
        }
    if resource_snapshot is not None:
        previous_snapshot = normalize_executor_resource_snapshot(metadata.get("resource_snapshot"))
        if (
            previous_snapshot is None
            or resource_snapshot.observed_at >= previous_snapshot.observed_at
        ):
            metadata["resource_snapshot"] = resource_snapshot.model_dump(
                mode="json",
                exclude={"freshness"},
            )
            metadata["resource_snapshot_received_at"] = received_at.isoformat()
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
