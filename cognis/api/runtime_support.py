"""Helpers for building API/runtime tool execution context."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

from cognis.core.executor_resolution import select_executor_for_agent
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ExecutorConfig, MCPServerConfig, ToolDefinition
from cognis.providers.executor.in_process import InProcessExecutorConnection
from cognis.tools.builtin.memory import memory_tools
from cognis.tools.builtin.orchestration import orchestration_tools
from cognis.tools.builtin.system import build_system_tool_handlers, system_tools
from cognis.tools.builtin.tool_output import tool_output_tools
from cognis.tools.builtin.workflow import workflow_tools
from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers
from cognis.tools.registry import RegisteredTool, ToolRegistry
from cognis.tools.skills import load_skill_tool_names

logger = get_logger(__name__)

RuntimeFactory = Callable[
    [AgentDefinition, str], Awaitable[tuple[ToolRegistry, Any, Callable[[], Awaitable[None]]]]
]


async def noop_cleanup() -> None:
    """No-op cleanup callback."""


def static_tool_definitions() -> list[ToolDefinition]:
    """Return all static tool definitions available to Cognis.

    Includes builtin controller tools, workflow tools, memory tools,
    and executor-native tools.
    """
    return [
        *system_tools(),
        *orchestration_tools(),
        *workflow_tools(),
        *memory_tools(),
        *tool_output_tools(),
        *executor_tool_definitions(),
    ]


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


def _build_handler_map(session_factory: Any, status_provider: Any) -> dict[str, Any]:
    """Build a combined handler map for all tool sources."""
    handlers: dict[str, Any] = {}
    handlers.update(build_system_tool_handlers(session_factory, status_provider))
    handlers.update(executor_tool_handlers())
    return handlers


def build_registry_with_handlers(
    tools: list[ToolDefinition],
    handler_map: dict[str, Any],
) -> ToolRegistry:
    """Build a ToolRegistry with actual handlers attached.

    This is the fix for the two-registry bug: registries must have handlers
    so that tool_execute() can dispatch them.
    """
    registry = ToolRegistry()
    for tool in tools:
        handler = None
        if tool.source.type in ("builtin", "executor"):
            handler = handler_map.get(tool.name)
        registry.register(RegisteredTool(definition=tool, handler=cast(Any, handler)))
    return registry


def build_static_registry(agent: AgentDefinition | None = None) -> ToolRegistry:
    """Build a static ToolRegistry for one agent's builtin tools.

    NOTE: This registry has handler=None for all tools. It is used only for
    tool listing/discovery, NOT for execution. For execution, use
    build_registry_with_handlers().
    """
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
    session_factory: Any,
) -> RuntimeFactory:
    """Create a per-step runtime factory.

    Resolves the executor from DB config, filters tools by executor's
    enabled set, and builds registries WITH handlers for execution.

    ``session_factory`` is passed explicitly to avoid monkey-patching it
    onto the providers container.
    """

    async def factory(
        agent: AgentDefinition,
        user_email: str,
    ) -> tuple[ToolRegistry, Any, Callable[[], Awaitable[None]]]:
        # Resolve executor config from DB
        executor_config = await _resolve_executor_config(providers, agent)
        enabled_tools = executor_config.get("enabled_tools") if executor_config else None
        enabled_groups = executor_config.get("enabled_tool_groups") if executor_config else None

        # Filter tools by agent config AND executor enablement
        agent_tools = select_static_tools(agent)
        if executor_config is not None:
            # Only include executor-native tools that are enabled on this executor.
            # Controller-side tools (builtin) are always available regardless.
            filtered: list[ToolDefinition] = []
            for tool in agent_tools:
                if tool.source.type in ("builtin",):
                    # Controller tools always pass through
                    filtered.append(tool)
                elif tool.source.type == "executor":
                    from cognis.core.executor_resolution import is_tool_enabled

                    if is_tool_enabled(tool, enabled_tools, enabled_groups):
                        filtered.append(tool)
                else:
                    filtered.append(tool)
            agent_tools = filtered

        mcp_servers = _parse_local_mcp_servers(agent)
        if mcp_servers:
            secrets = await providers.secrets.resolve_for_execution(agent, user_email)
            handle = await providers.executor.spawn(
                ExecutorConfig(
                    executor_id=f"controller_step_{uuid.uuid4().hex[:12]}",
                    tools=agent_tools,
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
            # Build registry WITH handlers so tool_execute() can dispatch
            handler_map = _build_handler_map(
                session_factory,
                getattr(providers.executor, "status_provider", None),
            )
            registry = build_registry_with_handlers(agent_tools, handler_map)
            connection = InProcessExecutorConnection(
                shared_connection.handle,
                registry,
                shared_connection.breaker,
                {"user_email": user_email},
            )
            return registry, connection, noop_cleanup

        return shared_registry, shared_connection, noop_cleanup

    return factory


async def _resolve_executor_config(
    providers: Any,
    agent: AgentDefinition,
) -> dict[str, Any] | None:
    """Resolve executor config from DB for an agent.

    Returns a dict with enabled_tools, enabled_tool_groups, etc.
    or None if no executor config is found.
    """
    session_factory = getattr(providers, "_session_factory", None)
    if session_factory is None:
        return None

    from cognis.store.queries import list_executors

    try:
        async with session_factory() as session:
            executors = await list_executors(session)
            if not executors:
                return None
            selected = select_executor_for_agent(
                executors, agent.execution if isinstance(agent.execution, dict) else None
            )
            if selected is None:
                return None
            return {
                "executor_id": selected.executor_id,
                "enabled_tools": selected.enabled_tools or [],
                "enabled_tool_groups": selected.enabled_tool_groups or [],
                "labels": selected.labels or {},
            }
    except Exception:
        logger.warning("Failed to resolve executor config from DB", exc_info=True)
        return None


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
