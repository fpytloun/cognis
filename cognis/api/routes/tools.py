"""Tool discovery routes."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta
from time import monotonic
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from cognis.api.common import (
    api_exception,
    require_current_user,
    require_owner_or_admin,
)
from cognis.api.models import (
    EffectiveToolItemResponse,
    EffectiveToolsExecutorResponse,
    EffectiveToolsPreviewRequest,
    EffectiveToolsResponse,
    EffectiveToolsStateResponse,
    ExecutorStatusResponse,
    IntarisMCPServerResponse,
    MCPServerCreateRequest,
    MCPServerResponse,
    MCPServerTestItemResponse,
    MCPServerTestResponse,
    MCPServerUpdateRequest,
    ToolResponse,
)
from cognis.api.runtime_support import select_static_tools
from cognis.api.serializers import agent_to_response, mcp_server_to_response, tool_to_response
from cognis.core.executor_policy import load_executor_policy
from cognis.core.executor_resolution import is_tool_enabled, select_executor_for_agent
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.tool import MCP_SERVER_IDS_KEY, ExecutorConfig, MCPServerConfig, ToolDefinition
from cognis.store.queries import (
    create_mcp_server,
    delete_mcp_server,
    get_agent,
    get_mcp_server,
    list_agents,
    list_executors,
    mcp_server_referenced_by_executors,
    update_mcp_server,
)
from cognis.store.queries import (
    list_mcp_servers as list_global_mcp_servers,
)
from cognis.tools.executor.definitions import executor_tool_definitions
from cognis.tools.mcp import StdioMCPClient, mcp_tools_to_definitions, resolve_secret_refs

router = APIRouter(tags=["tools"])


def _sanitize_mcp_error(error: Exception) -> str:
    return f"{error.__class__.__name__}: {str(error)[:300]}"


async def _discover_local_mcp_tools(
    request: Request,
    agent: AgentDefinition,
    user_email: str,
    *,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    raw_servers = []
    if isinstance(agent.tools, dict):
        raw_servers = agent.tools.get("mcp_servers", [])
    if not isinstance(raw_servers, list) or not raw_servers:
        return []

    servers = [
        MCPServerConfig.model_validate(item) for item in raw_servers if isinstance(item, dict)
    ]
    if not servers:
        return []

    secrets = await request.app.state.providers.secrets.resolve_for_execution(agent, user_email)
    handle = await request.app.state.providers.executor.spawn(
        ExecutorConfig(
            executor_id=f"controller_mcp_test_{uuid.uuid4().hex[:12]}",
            mcp_servers=servers,
            secrets=secrets,
            metadata={"user_email": user_email},
        )
    )
    try:
        connection = await request.app.state.providers.executor.get_executor(handle)
        tools = await asyncio.wait_for(connection.list_tools(), timeout=timeout_seconds)
    finally:
        await request.app.state.providers.executor.cancel(handle)
    return [tool for tool in tools if tool.get("source", {}).get("type") == "local_mcp"]


@router.get("/api/v1/tools", response_model=list[ToolResponse])
async def list_tools(request: Request) -> list[ToolResponse]:
    require_current_user(request)
    return [tool_to_response(tool) for tool in select_static_tools()]


@router.get("/api/v1/tools/executor", response_model=list[ToolResponse])
async def list_executor_tools(request: Request) -> list[ToolResponse]:
    """List executor-native tools with their definitions."""
    require_current_user(request)
    return [tool_to_response(tool) for tool in executor_tool_definitions()]


@router.get("/api/v1/agents/{agent_id}/effective-tools", response_model=EffectiveToolsResponse)
async def get_agent_effective_tools(request: Request, agent_id: str) -> EffectiveToolsResponse:
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, row.owner_email)
    agent = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    return await _resolve_effective_tools_response(
        request,
        agent,
        user_email=agent.owner_email,
    )


@router.post("/api/v1/agents/effective-tools/preview", response_model=EffectiveToolsResponse)
async def preview_effective_tools(
    request: Request, payload: EffectiveToolsPreviewRequest
) -> EffectiveToolsResponse:
    user = require_current_user(request)
    agent = AgentDefinition(
        agent_id=payload.agent_id or "preview",
        owner_email=user.email,
        name="Preview",
        skills=payload.skills,
        tools=payload.tools,
        permissions=AgentPermissions.model_validate(payload.permissions or {}),
        execution=payload.execution,
    )
    return await _resolve_effective_tools_response(request, agent, user_email=user.email)


@router.get("/api/v1/executor/status", response_model=ExecutorStatusResponse)
async def executor_status(request: Request) -> ExecutorStatusResponse:
    """Get executor status and capabilities."""
    require_current_user(request)
    executor = request.app.state.providers.executor
    active = await executor.list_active()
    health = await executor.health()
    native_tool_names = [t.name for t in executor_tool_definitions()]
    return ExecutorStatusResponse(
        executor_type="in_process",
        status=health.status,
        active_executors=len(active),
        capabilities={
            "inference": False,
            "native_tools_count": len(native_tool_names),
        },
        native_tools=native_tool_names,
    )


def _tool_identifier(tool: ToolDefinition) -> str:
    if tool.source.type == "local_mcp":
        server_id = tool.source.server_id or tool.source.server_name or "unknown"
        raw_name = tool.name.split("/", 1)[1] if "/" in tool.name else tool.name
        return f"mcp:{server_id}:{raw_name}"
    return f"builtin:{tool.name}"


def _tool_permission(agent: AgentDefinition, tool: ToolDefinition) -> str:
    if agent.permissions is None:
        return "evaluate"
    if isinstance(agent.permissions, dict):
        agent.permissions = AgentPermissions.model_validate(agent.permissions)
    tool_id = _tool_identifier(tool)
    if agent.permissions.tool_permissions and tool_id in agent.permissions.tool_permissions:
        return str(agent.permissions.tool_permissions[tool_id])
    return str(agent.permissions.resolve_permission(tool.name))


async def _discover_temp_mcp_tools(
    providers: Any,
    servers: list[MCPServerConfig],
    user_email: str,
) -> list[ToolDefinition]:
    if not servers:
        return []
    secret_names = {
        value[len("$secret:") :]
        for server in servers
        for value in server.env.values()
        if isinstance(value, str) and value.startswith("$secret:")
    }
    secrets: dict[str, str] = {}
    for name in secret_names:
        with contextlib.suppress(Exception):
            secrets[name] = await providers.secrets.get_secret(name, user_email)
    clients: list[StdioMCPClient] = []
    discovered: list[ToolDefinition] = []
    try:
        for server in servers:
            client = StdioMCPClient(server, env=resolve_secret_refs(server.env, secrets))
            await client.start()
            clients.append(client)
            tools = await client.list_tools()
            discovered.extend(
                mcp_tools_to_definitions(
                    server.name,
                    tools,
                    timeout_seconds=server.timeout_seconds,
                    server_id=server.server_id,
                )
            )
    finally:
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()
    return discovered


async def _resolve_effective_tools_response(
    request: Request,
    agent: AgentDefinition,
    *,
    user_email: str,
) -> EffectiveToolsResponse:
    session_factory = request.app.state.session_factory
    policy = await load_executor_policy(session_factory)
    warnings: list[str] = []

    async with session_factory() as session:
        executors = await list_executors(session, owner_email=user_email)
        selected = select_executor_for_agent(
            executors,
            agent.execution if isinstance(agent.execution, dict) else None,
            owner_email=user_email,
            policy=policy,
        )

    if selected is None:
        warnings.append("No executor could be resolved for this agent.")
        empty_state = EffectiveToolsStateResponse()
        return EffectiveToolsResponse(
            executor=EffectiveToolsExecutorResponse(selection_source="unresolved"),
            configured_state=empty_state,
            live_state=empty_state,
            warnings=warnings,
        )

    executor_summary = EffectiveToolsExecutorResponse(
        executor_id=selected.executor_id,
        executor_type=selected.executor_type,
        selection_source=(
            "explicit"
            if (agent.execution or {}).get("executor_id")
            else "selector"
            if (agent.execution or {}).get("executor_selector")
            else "default"
        ),
    )

    configured_tools: list[ToolDefinition] = []
    for tool in [tool for tool in select_static_tools(agent) if tool.category != "web"]:
        if tool.source.type == "builtin" or is_tool_enabled(
            tool, selected.enabled_tools or [], selected.enabled_tool_groups or []
        ):
            configured_tools.append(tool)

    config_ids = (selected.config or {}).get(MCP_SERVER_IDS_KEY, [])
    if isinstance(config_ids, list) and config_ids:
        if selected.executor_type == "in_process":
            mcp_servers: list[MCPServerConfig] = []
            async with session_factory() as session:
                for server_id in config_ids:
                    row = await get_mcp_server(session, str(server_id), owner_email=user_email)
                    if row is None or row.status != "active":
                        continue
                    mcp_servers.append(
                        MCPServerConfig(
                            server_id=row.server_id,
                            name=row.name,
                            transport=row.transport,
                            command=row.command,
                            url=row.url,
                            args=row.args or [],
                            env=row.env or {},
                            timeout_seconds=row.timeout_seconds,
                        )
                    )
            configured_tools.extend(
                await _discover_temp_mcp_tools(request.app.state.providers, mcp_servers, user_email)
            )
        elif selected.observed_tools:
            configured_tools.extend(
                [ToolDefinition.model_validate(item) for item in selected.observed_tools]
            )
        else:
            warnings.append("Executor has assigned MCP servers but no observed manifest yet.")

    # Resolve Intaris MCP tools assigned to this agent
    from cognis.api.runtime_support import _resolve_intaris_mcp_tools

    disabled_categories = set(
        (agent.tools or {}).get("disabled_categories", []) if isinstance(agent.tools, dict) else []
    )
    disabled_tools_set = set(
        (agent.tools or {}).get("disabled_tools", []) if isinstance(agent.tools, dict) else []
    )
    intaris_tools = await _resolve_intaris_mcp_tools(
        request.app.state.providers, agent, disabled_categories, disabled_tools_set
    )
    configured_tools.extend(intaris_tools)

    configured_items = [
        EffectiveToolItemResponse(
            tool_id=_tool_identifier(tool),
            name=tool.name,
            description=tool.description,
            category=tool.category,
            read_only=tool.read_only,
            source=tool.source.model_dump(mode="json"),
            permission=_tool_permission(agent, tool),
            enabled=True,
            timeout_seconds=tool.timeout_seconds,
            non_bypassable=tool.non_bypassable,
        )
        for tool in configured_tools
    ]

    live_items: list[EffectiveToolItemResponse] = []
    connected = False
    observed_at = selected.last_observed_at
    stale_after = observed_at + timedelta(seconds=45) if observed_at is not None else None
    if selected.executor_type in {"websocket", "subprocess"}:
        conn = request.app.state.providers.executor.websocket.get_connection(selected.executor_id)
        if conn is not None and selected.runtime_state == "active":
            connected = True
            for item in await conn.list_tools():
                tool = ToolDefinition.model_validate(item)
                live_items.append(
                    EffectiveToolItemResponse(
                        tool_id=_tool_identifier(tool),
                        name=tool.name,
                        description=tool.description,
                        category=tool.category,
                        read_only=tool.read_only,
                        source=tool.source.model_dump(mode="json"),
                        permission=_tool_permission(agent, tool),
                        enabled=True,
                        timeout_seconds=tool.timeout_seconds,
                        non_bypassable=tool.non_bypassable,
                    )
                )
        else:
            warnings.append(
                "Selected executor is offline or not ready; live tool state unavailable."
            )
    else:
        connected = True
        live_items = configured_items

    return EffectiveToolsResponse(
        executor=executor_summary,
        configured_state=EffectiveToolsStateResponse(
            tools=configured_items,
            connected=True,
            observed_at=observed_at,
            stale_after=stale_after,
        ),
        live_state=EffectiveToolsStateResponse(
            tools=live_items,
            connected=connected,
            observed_at=observed_at,
            stale_after=stale_after,
        ),
        warnings=warnings,
    )


@router.get("/api/v1/intaris/mcp/servers", response_model=list[IntarisMCPServerResponse])
async def list_intaris_mcp_servers(request: Request) -> list[IntarisMCPServerResponse]:
    """Auto-discover available MCP servers from Intaris."""
    require_current_user(request)
    guardrails = request.app.state.providers.guardrails
    servers = await guardrails.list_mcp_servers(enabled_only=True)
    return [
        IntarisMCPServerResponse(
            name=s.get("name", ""),
            transport=s.get("transport"),
            enabled=s.get("enabled", True),
            tools_count=len(s.get("tools_cache") or [])
            if isinstance(s.get("tools_cache"), list)
            else 0,
            agent_pattern=s.get("agent_pattern", "*"),
        )
        for s in servers
        if isinstance(s, dict) and s.get("name")
    ]


@router.get("/api/v1/agents/{agent_id}/tools", response_model=list[ToolResponse])
async def list_agent_tools(request: Request, agent_id: str) -> list[ToolResponse]:
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, agent.owner_email)
    tools = [tool_to_response(tool) for tool in select_static_tools(agent)]
    agent_definition = AgentDefinition.model_validate(agent_to_response(agent).model_dump())
    try:
        discovered = await _discover_local_mcp_tools(request, agent_definition, agent.owner_email)
    except Exception:
        discovered = []
    tools.extend(ToolResponse.model_validate(tool) for tool in discovered)
    return tools


@router.get("/api/v1/mcp/servers", response_model=list[MCPServerResponse])
async def list_mcp_servers(request: Request) -> list[MCPServerResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        agents = await list_agents(session, owner_email=user.email)

    servers: list[MCPServerResponse] = []
    seen: set[tuple[str, str]] = set()
    for agent in agents:
        tools = agent.tools or {}
        if isinstance(tools, dict):
            raw_local = tools.get("mcp_servers", [])
            if isinstance(raw_local, list):
                for server in raw_local:
                    if not isinstance(server, dict):
                        continue
                    name = str(server.get("name", ""))
                    key = ("local_mcp", name)
                    if not name or key in seen:
                        continue
                    seen.add(key)
                    servers.append(mcp_server_to_response(name, "local_mcp", server))
            raw_remote = tools.get("intaris_mcp_servers", [])
            if isinstance(raw_remote, list):
                for name in raw_remote:
                    if not isinstance(name, str):
                        continue
                    key = ("intaris_mcp", name)
                    if key in seen:
                        continue
                    seen.add(key)
                    servers.append(mcp_server_to_response(name, "intaris_mcp", {}))
    return servers


@router.post("/api/v1/agents/{agent_id}/mcp/test", response_model=MCPServerTestResponse)
async def test_agent_mcp_servers(request: Request, agent_id: str) -> MCPServerTestResponse:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, agent.owner_email)

    agent_definition = AgentDefinition.model_validate(agent_to_response(agent).model_dump())
    raw_servers = []
    if isinstance(agent_definition.tools, dict):
        raw_servers = agent_definition.tools.get("mcp_servers", [])
    if not isinstance(raw_servers, list) or not raw_servers:
        raise api_exception(400, "validation_error", "Agent has no local MCP servers configured")

    started_at = monotonic()
    try:
        discovered = await asyncio.wait_for(
            _discover_local_mcp_tools(request, agent_definition, agent.owner_email), timeout=30
        )
    except TimeoutError:
        return MCPServerTestResponse(
            ok=False,
            items=[
                MCPServerTestItemResponse(
                    name="mcp",
                    ok=False,
                    error_type="timeout",
                    error_detail="MCP server discovery timed out.",
                    duration_ms=int((monotonic() - started_at) * 1000),
                )
            ],
        )
    except Exception as exc:
        return MCPServerTestResponse(
            ok=False,
            items=[
                MCPServerTestItemResponse(
                    name="mcp",
                    ok=False,
                    error_type="runtime_error",
                    error_detail=_sanitize_mcp_error(exc),
                    duration_ms=int((monotonic() - started_at) * 1000),
                )
            ],
        )

    if not discovered:
        return MCPServerTestResponse(
            ok=False,
            items=[
                MCPServerTestItemResponse(
                    name="mcp",
                    ok=False,
                    error_type="no_tools_discovered",
                    error_detail="No MCP tools were discovered for this agent.",
                    duration_ms=int((monotonic() - started_at) * 1000),
                )
            ],
        )

    grouped: dict[str, list[str]] = {}
    for tool in discovered:
        source = tool.get("source", {})
        server_name = str(source.get("server_name") or "mcp")
        grouped.setdefault(server_name, []).append(str(tool.get("name", "")))

    items = [
        MCPServerTestItemResponse(
            name=server_name,
            ok=True,
            tools=tool_names,
            duration_ms=int((monotonic() - started_at) * 1000),
        )
        for server_name, tool_names in grouped.items()
    ]
    return MCPServerTestResponse(ok=all(item.ok for item in items), items=items)


# --- Global MCP Server CRUD ---

_SECRET_PATTERNS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL"}


def _redact_env(env: dict[str, str] | None) -> dict[str, str]:
    """Redact environment variable values that look like secrets."""
    if not env:
        return {}
    redacted: dict[str, str] = {}
    for key, value in env.items():
        upper_key = key.upper()
        if any(pat in upper_key for pat in _SECRET_PATTERNS) or value.startswith("$secret:"):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _mcp_row_to_response(row: Any) -> dict[str, Any]:
    """Convert an MCPServerRow to a response dict with env redaction."""
    from cognis.api.models import MCPServerConfigResponse as MCPResp

    return MCPResp(
        server_id=row.server_id,
        name=row.name,
        transport=row.transport,
        command=row.command,
        url=row.url,
        args=row.args or [],
        env=_redact_env(row.env),
        timeout_seconds=row.timeout_seconds,
        description=row.description,
        owner_email=row.owner_email,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    ).model_dump()


@router.get("/api/v1/mcp-servers")
async def list_mcp_servers_route(request: Request) -> list[dict[str, Any]]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_global_mcp_servers(session, owner_email=user.email)
    return [_mcp_row_to_response(r) for r in rows]


@router.get("/api/v1/mcp-servers/{server_id}")
async def get_mcp_server_route(request: Request, server_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_mcp_server(session, server_id, owner_email=user.email)
    if row is None:
        raise api_exception(404, "not_found", "MCP server not found")
    return _mcp_row_to_response(row)


@router.post("/api/v1/mcp-servers")
async def create_mcp_server_route(request: Request, body: MCPServerCreateRequest) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await create_mcp_server(
            session,
            server_id=body.server_id,
            name=body.name,
            transport=body.transport,
            command=body.command,
            url=body.url,
            args=body.args,
            env=body.env,
            timeout_seconds=body.timeout_seconds,
            description=body.description,
            owner_email=user.email,
        )
        await session.commit()
    return _mcp_row_to_response(row)


@router.put("/api/v1/mcp-servers/{server_id}")
async def update_mcp_server_route(
    request: Request, server_id: str, body: MCPServerUpdateRequest
) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        existing = await get_mcp_server(session, server_id, owner_email=user.email)
        if existing is None:
            raise api_exception(404, "not_found", "MCP server not found")
        updates = body.model_dump(exclude_unset=True)
        if isinstance(updates.get("env"), dict) and isinstance(existing.env, dict):
            preserved_env: dict[str, str] = {}
            for key, value in updates["env"].items():
                preserved_env[key] = existing.env.get(key) if value == "***" else value
            updates["env"] = preserved_env
        merged = {
            "name": updates.get("name", existing.name),
            "transport": updates.get("transport", existing.transport),
            "command": updates.get("command", existing.command),
            "url": updates.get("url", existing.url),
            "args": updates.get("args", existing.args or []),
            "env": updates.get("env", existing.env or {}),
            "timeout_seconds": updates.get("timeout_seconds", existing.timeout_seconds),
        }
        try:
            MCPServerConfig.model_validate(merged)
        except ValidationError as exc:
            raise api_exception(422, "validation_error", str(exc)) from exc
        row = await update_mcp_server(session, server_id, owner_email=user.email, **updates)
        if row is None:
            raise api_exception(404, "not_found", "MCP server not found")
        await session.commit()
    return _mcp_row_to_response(row)


@router.delete("/api/v1/mcp-servers/{server_id}")
async def delete_mcp_server_route(request: Request, server_id: str) -> dict[str, str]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        # Check for executor references before deleting
        referencing = await mcp_server_referenced_by_executors(
            session, server_id, owner_email=user.email
        )
        if referencing:
            raise api_exception(
                409,
                "referenced",
                f"MCP server is referenced by executor(s): {', '.join(referencing)}. "
                "Remove the assignment first.",
            )
        deleted = await delete_mcp_server(session, server_id, owner_email=user.email)
        if not deleted:
            raise api_exception(404, "not_found", "MCP server not found")
        await session.commit()
    return {"status": "deleted"}
