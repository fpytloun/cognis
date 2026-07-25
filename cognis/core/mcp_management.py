"""Owner-scoped MCP management shared by controller tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update

from cognis.core.mcp_oauth import MCPOAuthError
from cognis.models.tool import MCP_SERVER_IDS_KEY, MCPServerConfig, effective_mcp_auth_config
from cognis.ownership import is_shared_owner_email
from cognis.store.models import ExecutorRow, MCPServerRow
from cognis.store.queries import (
    create_mcp_server,
    get_executor_row,
    get_mcp_server,
    list_executors,
    list_mcp_servers,
    mcp_server_referenced_by_executors,
)


class MCPManagementError(ValueError):
    """Expected user-facing MCP management failure."""


ReconfigureCallback = Callable[[str, str], Awaitable[None]]
OAuthStatusCallback = Callable[[str, str], Awaitable[dict[str, Any] | None]]
OAuthDisconnectCallback = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class MCPManagementDependencies:
    session_factory: Any
    oauth_service: Any | None = None
    reconfigure_server: ReconfigureCallback | None = None
    reconfigure_executor: ReconfigureCallback | None = None
    oauth_status: OAuthStatusCallback | None = None
    oauth_disconnect: OAuthDisconnectCallback | None = None


_SECRET_PATTERNS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "AUTHORIZATION"}


def _redact_secret_mapping(values: dict[str, str] | None) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in (values or {}).items():
        if value.startswith("$secret:"):
            redacted[key] = value
        elif any(pattern in key.upper() for pattern in _SECRET_PATTERNS):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _server_payload(row: Any) -> dict[str, Any]:
    auth = effective_mcp_auth_config(row.auth_config, row.headers)
    auth_payload = auth.model_dump(mode="json", exclude_none=True)
    if auth_payload.get("client_secret_ref") and not str(
        auth_payload["client_secret_ref"]
    ).startswith("$secret:"):
        auth_payload["client_secret_ref"] = "***"
    if isinstance(auth_payload.get("authorization_params"), dict):
        auth_payload["authorization_params"] = _redact_secret_mapping(
            auth_payload["authorization_params"]
        )
    return {
        "server_id": row.server_id,
        "name": row.name,
        "description": row.description,
        "transport": row.transport,
        "command": row.command,
        "url": row.url,
        "args": list(row.args or []),
        "env": _redact_secret_mapping(row.env),
        "headers": _redact_secret_mapping(row.headers),
        "auth_config": auth_payload,
        "timeout_seconds": row.timeout_seconds,
        "status": row.status,
        "updated_at": row.updated_at.isoformat(),
    }


async def _owned_server(deps: MCPManagementDependencies, owner: str, server_id: str) -> Any:
    async with deps.session_factory() as session:
        row = await get_mcp_server(session, server_id, owner_email=owner, include_shared=False)
    if row is None:
        raise MCPManagementError("MCP server not found")
    return row


async def _visible_server(deps: MCPManagementDependencies, owner: str, server_id: str) -> Any:
    async with deps.session_factory() as session:
        row = await get_mcp_server(session, server_id, owner_email=owner, include_shared=True)
    if row is None:
        raise MCPManagementError("MCP server not found")
    return row


async def _owned_websocket_executor(
    deps: MCPManagementDependencies, owner: str, executor_id: str
) -> Any:
    async with deps.session_factory() as session:
        row = await get_executor_row(session, executor_id, owner_email=owner, include_shared=False)
    if row is None or row.executor_type != "websocket" or is_shared_owner_email(row.owner_email):
        raise MCPManagementError("Owned WebSocket executor not found")
    return row


async def handle_mcp_management_action(
    *,
    deps: MCPManagementDependencies,
    actor_email: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    action = str(arguments.get("action") or "")
    if action == "servers_list":
        async with deps.session_factory() as session:
            rows = await list_mcp_servers(session, owner_email=actor_email, include_shared=False)
        return {"servers": [_server_payload(row) for row in rows]}
    if action == "servers_get":
        return {
            "server": _server_payload(
                await _owned_server(deps, actor_email, str(arguments["server_id"]))
            )
        }
    if action == "executors_list":
        async with deps.session_factory() as session:
            rows = await list_executors(session, owner_email=actor_email, include_shared=False)
        return {
            "executors": [
                {
                    "executor_id": row.executor_id,
                    "name": row.name,
                    "runtime_state": row.runtime_state,
                    "desired_config_version": row.desired_config_version,
                    "applied_config_version": row.applied_config_version,
                }
                for row in rows
                if row.executor_type == "websocket"
            ]
        }
    if action == "servers_create":
        candidate = {
            key: arguments.get(key)
            for key in (
                "server_id",
                "name",
                "transport",
                "command",
                "url",
                "args",
                "env",
                "headers",
                "auth_config",
                "timeout_seconds",
            )
            if key in arguments
        }
        try:
            config = MCPServerConfig.model_validate(candidate)
        except ValueError as exc:
            raise MCPManagementError(str(exc)) from exc
        async with deps.session_factory() as session:
            row = await create_mcp_server(
                session,
                server_id=config.server_id,
                name=config.name,
                transport=config.transport,
                command=config.command,
                url=config.url,
                args=config.args,
                env=config.env,
                headers=config.headers,
                auth_config=config.auth_config.model_dump(mode="json"),
                timeout_seconds=config.timeout_seconds,
                description=arguments.get("description"),
                owner_email=actor_email,
            )
            await session.commit()
        return {"status": "created", "server": _server_payload(row)}
    if action == "servers_update":
        server_id = str(arguments["server_id"])
        existing = await _owned_server(deps, actor_email, server_id)
        expected = arguments.get("expected_updated_at")
        if expected and existing.updated_at.isoformat() != expected:
            raise MCPManagementError("MCP server changed; inspect it and retry")
        updates = {
            key: arguments[key]
            for key in (
                "name",
                "transport",
                "command",
                "url",
                "args",
                "env",
                "headers",
                "auth_config",
                "timeout_seconds",
                "description",
            )
            if key in arguments
        }
        for field in ("env", "headers"):
            if isinstance(updates.get(field), dict):
                current_values = getattr(existing, field) or {}
                updates[field] = {
                    key: current_values.get(key, "") if value == "***" else value
                    for key, value in updates[field].items()
                }
        if isinstance(updates.get("auth_config"), dict):
            current_auth = effective_mcp_auth_config(
                existing.auth_config, existing.headers
            ).model_dump(mode="json")
            next_auth = dict(updates["auth_config"])
            if next_auth.get("client_secret_ref") == "***":
                next_auth["client_secret_ref"] = current_auth.get("client_secret_ref")
            if isinstance(next_auth.get("authorization_params"), dict):
                current_params = current_auth.get("authorization_params") or {}
                next_auth["authorization_params"] = {
                    key: current_params.get(key, "") if value == "***" else value
                    for key, value in next_auth["authorization_params"].items()
                }
            updates["auth_config"] = next_auth
        merged = {
            key: updates.get(key, getattr(existing, key))
            for key in (
                "name",
                "transport",
                "command",
                "url",
                "args",
                "env",
                "headers",
                "auth_config",
                "timeout_seconds",
            )
        }
        try:
            MCPServerConfig.model_validate(merged)
        except ValueError as exc:
            raise MCPManagementError(str(exc)) from exc
        revision = _parse_revision(str(expected))
        async with deps.session_factory() as session:
            result = await session.execute(
                update(MCPServerRow)
                .where(
                    MCPServerRow.server_id == server_id,
                    MCPServerRow.owner_email == actor_email,
                    MCPServerRow.updated_at == revision,
                )
                .values(**updates)
            )
            if result.rowcount != 1:
                raise MCPManagementError("MCP server changed; inspect it and retry")
            await session.commit()
            row = await get_mcp_server(
                session, server_id, owner_email=actor_email, include_shared=False
            )
        if deps.reconfigure_server is not None:
            await deps.reconfigure_server(server_id, "mcp_management_update")
        return {"status": "updated", "server": _server_payload(row)}
    if action == "servers_delete":
        server_id = str(arguments["server_id"])
        await _owned_server(deps, actor_email, server_id)
        revision = _parse_revision(str(arguments["expected_updated_at"]))
        async with deps.session_factory() as session:
            locked_server = (
                await session.execute(
                    select(MCPServerRow)
                    .where(
                        MCPServerRow.server_id == server_id,
                        MCPServerRow.owner_email == actor_email,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_server is None:
                raise MCPManagementError("MCP server not found")
            await session.execute(
                select(ExecutorRow).where(ExecutorRow.owner_email == actor_email).with_for_update()
            )
            references = await mcp_server_referenced_by_executors(
                session, server_id, owner_email=actor_email, include_shared=False
            )
            if references:
                raise MCPManagementError(
                    f"MCP server is assigned to executor(s): {', '.join(references)}"
                )
            result = await session.execute(
                delete(MCPServerRow).where(
                    MCPServerRow.server_id == server_id,
                    MCPServerRow.owner_email == actor_email,
                    MCPServerRow.updated_at == revision,
                )
            )
            if result.rowcount != 1:
                raise MCPManagementError("MCP server changed; inspect it and retry")
            await session.commit()
        return {"status": "deleted", "server_id": server_id}
    if action.startswith("assignments_"):
        executor_id = str(arguments["executor_id"])
        executor = await _owned_websocket_executor(deps, actor_email, executor_id)
        if action == "assignments_get":
            return {
                "executor_id": executor_id,
                "server_ids": list((executor.config or {}).get(MCP_SERVER_IDS_KEY, [])),
                "desired_config_version": executor.desired_config_version,
                "applied_config_version": executor.applied_config_version,
            }
        expected = int(arguments["expected_config_version"])
        requested = list(dict.fromkeys(str(item) for item in arguments["server_ids"]))
        async with deps.session_factory() as session:
            requested_rows = (
                (
                    await session.execute(
                        select(MCPServerRow)
                        .where(
                            MCPServerRow.server_id.in_(sorted(set(requested))),
                            MCPServerRow.owner_email == actor_email,
                        )
                        .order_by(MCPServerRow.server_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if {item.server_id for item in requested_rows} != set(requested):
                raise MCPManagementError("MCP server not found")
            row = (
                await session.execute(
                    select(ExecutorRow)
                    .where(
                        ExecutorRow.executor_id == executor_id,
                        ExecutorRow.owner_email == actor_email,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or int(row.desired_config_version or 0) != expected:
                raise MCPManagementError("Executor configuration changed; inspect it and retry")
            current = list((row.config or {}).get(MCP_SERVER_IDS_KEY, []))
            if action == "assignments_set":
                updated = requested
            elif action == "assignments_add":
                updated = list(dict.fromkeys([*current, *requested]))
            elif action == "assignments_remove":
                removed = set(requested)
                updated = [item for item in current if item not in removed]
            else:
                raise MCPManagementError(f"Unknown action: {action}")
            changed = updated != current
            if changed:
                config = dict(row.config or {})
                config[MCP_SERVER_IDS_KEY] = updated
                desired = expected + 1
                result = await session.execute(
                    update(ExecutorRow)
                    .where(
                        ExecutorRow.executor_id == executor_id,
                        ExecutorRow.owner_email == actor_email,
                        ExecutorRow.desired_config_version == expected,
                    )
                    .values(config=config, desired_config_version=desired)
                )
                if result.rowcount != 1:
                    raise MCPManagementError("Executor configuration changed; inspect it and retry")
                await session.commit()
            else:
                desired = expected
        if changed and deps.reconfigure_executor is not None:
            await deps.reconfigure_executor(executor_id, "mcp_management_assignment")
        return {
            "status": "updated" if changed else "unchanged",
            "executor_id": executor_id,
            "server_ids": updated,
            "desired_config_version": desired,
        }
    if action == "oauth_authorize":
        server_id = str(arguments["server_id"])
        await _visible_server(deps, actor_email, server_id)
        if deps.oauth_service is None:
            raise MCPManagementError("MCP OAuth service is unavailable")
        try:
            started = await deps.oauth_service.start_authorization(
                user_email=actor_email, server_id=server_id
            )
        except MCPOAuthError as exc:
            raise MCPManagementError(str(exc)) from exc
        return {
            "status": "authorization_pending",
            "authorization_url": started.authorization_url,
            "transaction_id": started.transaction_id,
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
            "expires_at": started.expires_at.isoformat(),
        }
    if action in {"oauth_status", "oauth_disconnect"}:
        server_id = str(arguments["server_id"])
        await _visible_server(deps, actor_email, server_id)
        if action == "oauth_disconnect":
            if deps.oauth_disconnect is None:
                raise MCPManagementError("MCP OAuth disconnect is unavailable")
            return await deps.oauth_disconnect(actor_email, server_id)
        if deps.oauth_status is None:
            raise MCPManagementError("MCP OAuth status is unavailable")
        payload = await deps.oauth_status(actor_email, server_id)
        if payload is None:
            raise MCPManagementError("MCP server not found")
        return payload
    raise MCPManagementError(f"Unknown action: {action}")


def _parse_revision(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MCPManagementError("expected_updated_at must be an ISO-8601 timestamp") from exc
