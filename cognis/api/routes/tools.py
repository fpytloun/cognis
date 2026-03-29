"""Tool discovery routes."""

from __future__ import annotations

import asyncio
import uuid
from time import monotonic
from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user, require_owner_or_admin
from cognis.api.models import (
    ExecutorStatusResponse,
    IntarisMCPServerResponse,
    MCPServerResponse,
    MCPServerTestItemResponse,
    MCPServerTestResponse,
    ToolResponse,
)
from cognis.api.runtime_support import select_static_tools
from cognis.api.serializers import agent_to_response, mcp_server_to_response, tool_to_response
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ExecutorConfig, MCPServerConfig
from cognis.store.queries import get_agent, list_agents
from cognis.tools.executor.definitions import executor_tool_definitions

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
