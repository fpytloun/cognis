"""Tool discovery routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_current_user, require_owner_or_admin
from cognis.api.models import MCPServerResponse, ToolResponse
from cognis.api.runtime_support import select_static_tools
from cognis.api.serializers import mcp_server_to_response, tool_to_response
from cognis.store.queries import get_agent, list_agents

router = APIRouter(tags=["tools"])


@router.get("/api/v1/tools", response_model=list[ToolResponse])
async def list_tools(request: Request) -> list[ToolResponse]:
    require_current_user(request)
    return [tool_to_response(tool) for tool in select_static_tools()]


@router.get("/api/v1/agents/{agent_id}/tools", response_model=list[ToolResponse])
async def list_agent_tools(request: Request, agent_id: str) -> list[ToolResponse]:
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    require_owner_or_admin(request, agent.owner_email)
    return [tool_to_response(tool) for tool in select_static_tools(agent)]


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
