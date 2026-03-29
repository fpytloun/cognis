"""Helpers for building API/runtime tool execution context."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from cognis.models.agent import AgentDefinition
from cognis.models.tool import ExecutorConfig, MCPServerConfig, ToolDefinition
from cognis.providers.executor.in_process import InProcessExecutorConnection
from cognis.tools.builtin.orchestration import orchestration_tools
from cognis.tools.builtin.system import system_tools
from cognis.tools.registry import RegisteredTool, ToolRegistry
from cognis.tools.skills import load_skill_tool_names

RuntimeFactory = Callable[
    [AgentDefinition, str], Awaitable[tuple[ToolRegistry, Any, Callable[[], Awaitable[None]]]]
]


async def noop_cleanup() -> None:
    """No-op cleanup callback."""


def static_tool_definitions() -> list[ToolDefinition]:
    """Return the static builtin tool definitions available to Cognis."""
    return [*system_tools(), *orchestration_tools()]


def select_static_tools(agent: Any | None = None) -> list[ToolDefinition]:
    """Filter static builtin tools for an agent definition."""
    definitions = static_tool_definitions()
    if agent is None or not isinstance(agent.tools, dict):
        return definitions

    builtin_allow = agent.tools.get("builtin_tools")
    allowlist = builtin_allow if isinstance(builtin_allow, list) else None
    skill_tool_names = load_skill_tool_names(agent)
    allow_all_builtins = allowlist is None or "*" in allowlist
    delegation_enabled = bool(agent.tools.get("delegation_tools", True))

    selected: list[ToolDefinition] = []
    for tool in definitions:
        if tool.category == "orchestration":
            if delegation_enabled:
                selected.append(tool)
            continue
        if (
            allow_all_builtins
            or (allowlist is not None and tool.name in allowlist)
            or tool.name in skill_tool_names
        ):
            selected.append(tool)
    return selected


def build_static_registry(agent: AgentDefinition | None = None) -> ToolRegistry:
    """Build a static ToolRegistry for one agent's builtin tools."""
    registry = ToolRegistry()
    for tool in select_static_tools(agent):
        registry.register(RegisteredTool(definition=tool))
    return registry


async def build_shared_runtime(
    providers: Any,
) -> tuple[ToolRegistry, Any, Callable[[], Awaitable[None]]]:
    """Build the shared builtin runtime used as the template for step runtimes."""
    tools = static_tool_definitions()
    handle = await providers.executor.spawn(
        ExecutorConfig(
            executor_id="controller_shared_builtin",
            tools=tools,
            metadata={},
        )
    )
    connection = await providers.executor.get_executor(handle)
    registry = build_static_registry()

    async def cleanup() -> None:
        await providers.executor.cancel(handle)

    return registry, connection, cleanup


def build_step_runtime_factory(
    *,
    providers: Any,
    shared_registry: ToolRegistry,
    shared_connection: Any,
) -> RuntimeFactory:
    """Create a per-step runtime factory.

    For builtin-only steps we clone the shared in-process runtime with
    user-scoped metadata. When local MCP servers are configured we spawn a
    dedicated ephemeral runtime for that step.
    """

    async def factory(
        agent: AgentDefinition,
        user_email: str,
    ) -> tuple[ToolRegistry, Any, Callable[[], Awaitable[None]]]:
        mcp_servers = _parse_local_mcp_servers(agent)
        if mcp_servers:
            secrets = await providers.secrets.resolve_for_execution(agent, user_email)
            handle = await providers.executor.spawn(
                ExecutorConfig(
                    executor_id=f"controller_step_{uuid.uuid4().hex[:12]}",
                    tools=select_static_tools(agent),
                    mcp_servers=mcp_servers,
                    secrets=secrets,
                    metadata={"user_email": user_email},
                )
            )
            connection = await providers.executor.get_executor(handle)
            registry = getattr(connection, "registry", shared_registry)

            async def cleanup() -> None:
                await providers.executor.cancel(handle)

            return registry, connection, cleanup

        if isinstance(shared_connection, InProcessExecutorConnection):
            filtered_registry = build_static_registry(agent)
            connection = InProcessExecutorConnection(
                shared_connection.handle,
                filtered_registry,
                shared_connection.breaker,
                {"user_email": user_email},
            )
            return filtered_registry, connection, noop_cleanup

        return shared_registry, shared_connection, noop_cleanup

    return factory


def _parse_local_mcp_servers(agent: AgentDefinition) -> list[MCPServerConfig]:
    if not isinstance(agent.tools, dict):
        return []
    raw_servers = agent.tools.get("mcp_servers")
    if not isinstance(raw_servers, list):
        return []
    servers: list[MCPServerConfig] = []
    for item in raw_servers:
        if not isinstance(item, dict):
            continue
        servers.append(MCPServerConfig.model_validate(item))
    return servers
