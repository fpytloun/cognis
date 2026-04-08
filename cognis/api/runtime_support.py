"""Helpers for building API/runtime tool execution context."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, cast

from prometheus_client import Counter, Histogram

from cognis.api.tool_inventory import (
    build_intaris_tool_definition,
    extract_intaris_aggregated_raw_tool_name,
    extract_intaris_aggregated_server_name,
)
from cognis.core.executor_policy import load_executor_policy
from cognis.core.executor_resolution import select_executor_for_agent
from cognis.core.runtime import (
    ResolvedStepRuntime,
    build_local_executor_environment,
    environment_from_metadata,
)
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.tool import (
    MCP_SERVER_IDS_KEY,
    ExecutorConfig,
    MCPServerConfig,
    ToolDefinition,
    stable_tool_id,
)
from cognis.providers.executor.in_process import InProcessExecutorConnection
from cognis.tools.builtin.datetime_tools import build_datetime_tool_handlers, datetime_tools
from cognis.tools.builtin.image import image_tools
from cognis.tools.builtin.memory import memory_tools
from cognis.tools.builtin.orchestration import orchestration_tools
from cognis.tools.builtin.skill_management import skill_management_tools
from cognis.tools.builtin.system import build_system_tool_handlers, system_tools
from cognis.tools.builtin.tool_output import tool_output_tools
from cognis.tools.builtin.workflow import workflow_tools
from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers
from cognis.tools.registry import RegisteredTool, ToolRegistry
from cognis.tools.skills import (
    build_available_skills_metadata,
    load_skill_tool_names,
    resolve_skills_for_agent,
    skill_tools_to_definitions,
)

logger = get_logger(__name__)

INTARIS_MCP_FALLBACKS = Counter(
    "cognis_intaris_mcp_fallbacks_total",
    "Assigned Intaris MCP servers targeted by fallback to cached server manifests.",
)
INTARIS_MCP_COLLISIONS = Counter(
    "cognis_intaris_mcp_collision_prunes_total",
    "Intaris MCP tools skipped due to runtime name collisions.",
)
INTARIS_MCP_SKIPPED_ROWS = Counter(
    "cognis_intaris_mcp_skipped_rows_total",
    "Malformed or unusable aggregated Intaris MCP rows skipped during resolution.",
)
INTARIS_MCP_LIST_FAILURES = Counter(
    "cognis_intaris_mcp_list_failures_total",
    "Failures while listing aggregated Intaris MCP tools.",
)
INTARIS_MCP_SERVER_LIST_FAILURES = Counter(
    "cognis_intaris_mcp_server_list_failures_total",
    "Failures while listing Intaris MCP server manifests for compatibility fallback.",
)
INTARIS_MCP_RESOLUTION_LATENCY = Histogram(
    "cognis_intaris_mcp_resolution_latency_seconds",
    "Latency of Intaris MCP inventory resolution.",
)

RuntimeFactory = Callable[[AgentDefinition, str], Awaitable[ResolvedStepRuntime]]


@dataclass(slots=True)
class IntarisMCPResolutionResult:
    tools: list[ToolDefinition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback_used: bool = False
    collision_count: int = 0


@dataclass(slots=True)
class RemoteInventoryMergeResult:
    tools: list[ToolDefinition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    collision_count: int = 0
    intaris_fallback_used: bool = False


async def noop_cleanup() -> None:
    """No-op cleanup callback."""


def static_tool_definitions() -> list[ToolDefinition]:
    """Return all static tool definitions available to Cognis.

    Includes builtin controller tools, workflow tools, memory tools,
    executor-native tools, and a maximal set of web tools (for
    discovery/listing — actual web tools sent to the LLM are filtered
    per-session based on available backends).
    """
    from cognis.tools.executor.web.definitions import web_tool_definitions

    # Include all web tools for discovery (as if all backends were available)
    all_web = web_tool_definitions(["direct", "tavily", "brave"])
    from cognis.tools.builtin.schedule import schedule_tools

    return [
        *system_tools(),
        *datetime_tools(),
        *orchestration_tools(),
        *workflow_tools(),
        *memory_tools(),
        *tool_output_tools(),
        *image_tools(),
        *skill_management_tools(),
        *schedule_tools(),
        *executor_tool_definitions(),
        *all_web,
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

    disabled_categories = set(agent.tools.get("disabled_categories") or [])
    disabled_tools = set(agent.tools.get("disabled_tools") or [])

    selected: list[ToolDefinition] = []
    for tool in definitions:
        # Agent-level disable takes precedence
        if (
            tool.category in disabled_categories
            or tool.name in disabled_tools
            or stable_tool_id(tool) in disabled_tools
        ):
            continue
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
    handlers.update(build_datetime_tool_handlers())
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
        elif tool.source.type == "skill" and getattr(tool, "execution_metadata", None):
            # Build skill handler dynamically from execution metadata
            from cognis.providers.executor.in_process import _build_skill_handler

            handler = _build_skill_handler(tool)
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
) -> ResolvedStepRuntime:
    """Build the shared builtin runtime used as the template for step runtimes."""
    session_factory = getattr(providers, "_session_factory", None)
    if session_factory is not None:
        policy = await load_executor_policy(session_factory)
        if not policy.allow_in_process:
            logger.info("Shared in-process executor disabled by policy; using static-only template")
            return ResolvedStepRuntime(
                tool_registry=build_static_registry(),
                executor_connection=None,
                cleanup=noop_cleanup,
                executor_environment=build_local_executor_environment(
                    executor_type="in_process",
                    source="shared_runtime_disabled",
                ),
            )
    tools = static_tool_definitions()
    handle = await providers.executor.spawn(
        ExecutorConfig(
            executor_id="controller_shared_builtin",
            tools=tools,
            metadata={"executor_type": "in_process"},
        )
    )
    connection = await providers.executor.get_executor(handle)
    registry = build_static_registry()

    async def cleanup() -> None:
        await providers.executor.cancel(handle)

    return ResolvedStepRuntime(
        tool_registry=registry,
        executor_connection=connection,
        cleanup=cleanup,
        executor_environment=build_local_executor_environment(
            executor_id=handle.executor_id,
            executor_type=handle.executor_type,
            source="shared_runtime",
        ),
    )


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
    ) -> ResolvedStepRuntime:
        session_factory_for_policy = getattr(providers, "_session_factory", None)
        policy = (
            await load_executor_policy(session_factory_for_policy)
            if session_factory_for_policy is not None
            else None
        )
        # Resolve executor config from DB
        executor_config = await _resolve_executor_config(providers, agent, user_email)
        enabled_tools = executor_config.get("enabled_tools") if executor_config else None
        enabled_groups = executor_config.get("enabled_tool_groups") if executor_config else None

        # Build runtime metadata: user context + executor DB config (LSP settings, etc.)
        db_config = executor_config.get("config", {}) if executor_config else {}
        runtime_metadata = {"user_email": user_email, **db_config}

        # Inject web backend config (backend name + API keys)
        web_config = await _resolve_web_config(providers, user_email)
        runtime_metadata.update(web_config)

        # Filter tools by agent config AND executor enablement.
        # Exclude web-category tools — they are injected dynamically below
        # based on available backends.
        agent_tools = [t for t in select_static_tools(agent) if t.category != "web"]

        # Add dynamic web tool definitions based on available backends
        from cognis.tools.executor.web.definitions import web_tool_definitions

        agent_tools.extend(web_tool_definitions(web_config["web_available_backends"]))

        # Resolve DB-backed skills for this agent and inject:
        # 1. Compact metadata into agent.skills for context assembly
        # 2. Executable skill tool definitions into agent_tools
        try:
            async with session_factory() as db_session:
                resolved_skills = await resolve_skills_for_agent(
                    db_session, agent, owner_email=user_email
                )
            if resolved_skills.skills:
                # Build compact metadata for the immutable prompt prefix
                metadata = build_available_skills_metadata(resolved_skills)
                if metadata:
                    if not isinstance(agent.skills, dict):
                        agent.skills = {}
                    agent.skills["_available_skills_metadata"] = metadata

                # Add executable skill tools to the agent tool set
                skill_tool_defs = skill_tools_to_definitions(resolved_skills)
                agent_tools.extend(skill_tool_defs)
        except Exception:
            logger.warning(
                "Failed to resolve DB-backed skills for agent",
                extra={"extra_data": {"agent_id": agent.agent_id}},
                exc_info=True,
            )

        if executor_config is not None:
            # Only include executor-native tools that are enabled on this executor.
            # Controller-side tools (builtin) are always available regardless.
            disabled_categories = (
                set(agent.tools.get("disabled_categories") or [])
                if isinstance(agent.tools, dict)
                else set()
            )
            disabled_tools = (
                set(agent.tools.get("disabled_tools") or [])
                if isinstance(agent.tools, dict)
                else set()
            )
            filtered: list[ToolDefinition] = []
            for tool in agent_tools:
                if (
                    tool.category in disabled_categories
                    or tool.name in disabled_tools
                    or stable_tool_id(tool) in disabled_tools
                ):
                    continue
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

        resolved_type = (
            executor_config.get("executor_type", "in_process") if executor_config else "in_process"
        )

        # Resolve MCP servers for local in-process execution first, then legacy inline fallback.
        # Remote executors advertise executor-assigned MCP tools through tool.list.
        mcp_servers: list[MCPServerConfig] = []
        if resolved_type == "in_process":
            mcp_servers = await _resolve_executor_mcp_servers(executor_config, session_factory)
        if not mcp_servers:
            mcp_servers = _parse_local_mcp_servers(agent)
        if mcp_servers:
            secrets = await providers.secrets.resolve_for_execution(agent, user_email)
            handle = await providers.executor.spawn(
                ExecutorConfig(
                    executor_id=f"controller_step_{uuid.uuid4().hex[:12]}",
                    tools=agent_tools,
                    mcp_servers=mcp_servers,
                    secrets=secrets,
                    metadata=runtime_metadata,
                )
            )
            connection = await providers.executor.get_executor(handle)
            registry = getattr(connection, "registry", shared_registry)

            async def cleanup() -> None:
                await providers.executor.cancel(handle)

            return ResolvedStepRuntime(
                tool_registry=registry,
                executor_connection=connection,
                cleanup=cleanup,
                executor_environment=build_local_executor_environment(
                    executor_id=handle.executor_id,
                    executor_type=handle.executor_type,
                    source="in_process_mcp_runtime",
                ),
            )

        if resolved_type in ("websocket", "subprocess"):
            # Remote executor — merge executor-advertised tools with
            # controller-side builtin tools (memory, orchestration, etc.)
            # and Intaris MCP tools.  Executor-native and web tools come
            # from the executor; everything else is handled locally by
            # the controller's ToolRouter.
            from cognis.providers.executor.websocket import WebSocketExecutorProvider

            ws_provider: WebSocketExecutorProvider = providers.executor.websocket
            executor_id = executor_config.get("executor_id", "") if executor_config else ""
            conn = ws_provider.get_connection(executor_id)
            runtime_ready = bool(
                executor_config is not None
                and executor_config.get("runtime_state", "offline") in {"active", "degraded"}
                and int(executor_config.get("desired_config_version", 0) or 0)
                == int(executor_config.get("applied_config_version", 0) or 0)
            )
            if conn is not None and runtime_ready:
                try:
                    disabled_categories = (
                        set(agent.tools.get("disabled_categories") or [])
                        if isinstance(agent.tools, dict)
                        else set()
                    )
                    disabled_tools = (
                        set(agent.tools.get("disabled_tools") or [])
                        if isinstance(agent.tools, dict)
                        else set()
                    )

                    remote_tools = await conn.list_tools()
                    merge_result = await _merge_remote_runtime_inventory(
                        remote_tools_data=remote_tools,
                        agent_tools=agent_tools,
                        providers=providers,
                        agent=agent,
                        disabled_categories=disabled_categories,
                        disabled_tools=disabled_tools,
                    )
                    remote_registry = ToolRegistry()
                    for tool in merge_result.tools:
                        remote_registry.register(RegisteredTool(definition=tool))
                    for warning in merge_result.warnings:
                        logger.warning(
                            "Remote runtime inventory warning",
                            extra={
                                "extra_data": {
                                    "executor_id": executor_id,
                                    "warning": warning,
                                }
                            },
                        )

                    # Diagnostics
                    all_tools = remote_registry.list_tools()
                    categories = {t.category for t in all_tools}
                    sources = {t.source.type for t in all_tools}
                    logger.info(
                        "Remote executor tool registry assembled",
                        extra={
                            "extra_data": {
                                "executor_id": executor_id,
                                "total_tools": len(all_tools),
                                "categories": sorted(categories),
                                "sources": sorted(sources),
                                "remote_executor_tools": sum(
                                    1 for t in all_tools if t.source.type == "executor"
                                ),
                                "builtin_tools": sum(
                                    1 for t in all_tools if t.source.type == "builtin"
                                ),
                                "intaris_mcp_tools": sum(
                                    1 for t in all_tools if t.source.type == "intaris_mcp"
                                ),
                                "collision_count": merge_result.collision_count,
                                "intaris_fallback_used": merge_result.intaris_fallback_used,
                            }
                        },
                    )
                    env_snapshot = environment_from_metadata(
                        ws_provider.get_handle_metadata(executor_id),
                        executor_id=executor_id,
                        executor_type=resolved_type,
                        fallback_source="remote_executor_metadata",
                    )
                    return ResolvedStepRuntime(
                        tool_registry=remote_registry,
                        executor_connection=conn,
                        cleanup=noop_cleanup,
                        executor_environment=env_snapshot,
                    )
                except Exception:
                    logger.warning(
                        "Failed to get tools from remote executor, falling back to in-process",
                        extra={"extra_data": {"executor_id": executor_id}},
                    )

        if isinstance(shared_connection, InProcessExecutorConnection) and not (
            policy is not None and not policy.allow_in_process
        ):
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
                runtime_metadata,
            )
            return ResolvedStepRuntime(
                tool_registry=registry,
                executor_connection=connection,
                cleanup=noop_cleanup,
                executor_environment=build_local_executor_environment(
                    executor_id=getattr(shared_connection.handle, "executor_id", None),
                    executor_type=getattr(shared_connection.handle, "executor_type", "in_process"),
                    source="shared_in_process_runtime",
                ),
            )

        if shared_connection is None:
            raise RuntimeError("No eligible executor is available for this agent")
        return ResolvedStepRuntime(
            tool_registry=shared_registry,
            executor_connection=shared_connection,
            cleanup=noop_cleanup,
            executor_environment=build_local_executor_environment(
                executor_id=getattr(
                    getattr(shared_connection, "handle", None), "executor_id", None
                ),
                executor_type=getattr(
                    getattr(shared_connection, "handle", None), "executor_type", "in_process"
                ),
                source="shared_runtime_fallback",
            ),
        )

    return factory


async def _resolve_executor_config(
    providers: Any,
    agent: AgentDefinition,
    user_email: str,
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
        policy = await load_executor_policy(session_factory)
        async with session_factory() as session:
            executors = await list_executors(session, owner_email=user_email)
            if not executors:
                return None
            selected = select_executor_for_agent(
                executors,
                agent.execution if isinstance(agent.execution, dict) else None,
                owner_email=user_email,
                policy=policy,
            )
            if selected is None:
                return None
            return {
                "executor_id": selected.executor_id,
                "executor_type": selected.executor_type,
                "enabled_tools": selected.enabled_tools or [],
                "enabled_tool_groups": selected.enabled_tool_groups or [],
                "labels": selected.labels or {},
                "config": selected.config or {},
                "owner_email": selected.owner_email,
                "desired_config_version": selected.desired_config_version,
                "applied_config_version": selected.applied_config_version,
                "observed_tools": selected.observed_tools or [],
                "last_observed_at": selected.last_observed_at,
                "runtime_state": selected.runtime_state,
            }
    except Exception:
        logger.warning("Failed to resolve executor config from DB", exc_info=True)
        return None


async def _resolve_web_config(
    providers: Any,
    user_email: str,
) -> dict[str, Any]:
    """Resolve web backend configuration from settings and secrets.

    Returns a dict with keys suitable for merging into runtime_metadata:
    - web_backend: str — default backend name ("direct", "tavily", "brave")
    - web_secrets: dict — API keys for configured backends
    - web_available_backends: list[str] — backends that have API keys configured
    """
    web_backend = "direct"
    web_secrets: dict[str, str] = {}

    # Read web.backend setting from DB
    session_factory = getattr(providers, "_session_factory", None)
    if session_factory is not None:
        try:
            from cognis.store.queries import get_setting_value

            async with session_factory() as session:
                value = await get_setting_value(session, "web.backend", "direct")
                if isinstance(value, str):
                    web_backend = value
        except Exception:
            logger.debug("web: failed to read web.backend setting", exc_info=True)

    # Read API keys from secrets provider
    if hasattr(providers, "secrets") and hasattr(providers.secrets, "get_secret"):
        for secret_name in ("tavily_api_key", "brave_api_key"):
            try:
                value = await providers.secrets.get_secret(secret_name, user_email)
                if value:
                    web_secrets[secret_name] = value
            except KeyError:
                pass  # Secret not configured — expected
            except Exception:
                logger.debug("web: failed to read secret %s", secret_name)

    available = ["direct"]
    if web_secrets.get("tavily_api_key"):
        available.append("tavily")
    if web_secrets.get("brave_api_key"):
        available.append("brave")

    return {
        "web_backend": web_backend,
        "web_secrets": web_secrets,
        "web_available_backends": available,
    }


async def _resolve_executor_mcp_servers(
    executor_config: dict[str, Any] | None,
    session_factory: Any,
) -> list[MCPServerConfig]:
    """Resolve MCP servers assigned to an executor via config.mcp_server_ids."""
    from cognis.store.queries import get_mcp_server

    if not executor_config:
        return []
    config = executor_config.get("config") or {}
    server_ids = config.get(MCP_SERVER_IDS_KEY, [])
    if not isinstance(server_ids, list) or not server_ids:
        return []

    servers: list[MCPServerConfig] = []
    async with session_factory() as session:
        for sid in server_ids:
            row = await get_mcp_server(
                session,
                str(sid),
                owner_email=executor_config.get("owner_email") if executor_config else None,
            )
            if row is None:
                logger.warning(
                    "MCP server not found",
                    extra={"extra_data": {"server_id": sid}},
                )
                continue
            if row.status != "active":
                continue
            servers.append(
                MCPServerConfig(
                    name=row.name,
                    transport=row.transport,
                    command=row.command,
                    url=row.url,
                    args=row.args or [],
                    env=row.env or {},
                    timeout_seconds=row.timeout_seconds,
                )
            )
    if servers:
        logger.debug(
            "Resolved executor MCP servers",
            extra={"extra_data": {"count": len(servers)}},
        )
    return servers


def _parse_local_mcp_servers(agent: AgentDefinition) -> list[MCPServerConfig]:
    """Parse legacy inline MCP server configs from agent.tools.mcp_servers."""
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


async def _merge_remote_runtime_inventory(
    *,
    remote_tools_data: list[dict[str, Any]],
    agent_tools: list[ToolDefinition],
    providers: Any,
    agent: AgentDefinition,
    disabled_categories: set[str],
    disabled_tools: set[str],
    intaris_result: IntarisMCPResolutionResult | None = None,
) -> RemoteInventoryMergeResult:
    """Build merged runtime-visible inventory for remote executors."""
    warnings: list[str] = []
    merged: list[ToolDefinition] = []
    merged_names: set[str] = set()
    collision_count = 0

    remote_defs: list[ToolDefinition] = []
    for tool_data in remote_tools_data:
        tool_def = ToolDefinition.model_validate(tool_data)
        if (
            tool_def.category in disabled_categories
            or tool_def.name in disabled_tools
            or stable_tool_id(tool_def) in disabled_tools
        ):
            continue
        remote_defs.append(tool_def)
    remote_defs.sort(
        key=lambda tool: (
            _tool_collision_identity(tool),
            tool.source.type,
            tool.source.server_name or "",
            tool.source.raw_tool_name or tool.name,
        )
    )
    for tool in remote_defs:
        if tool.name in merged_names:
            collision_count += 1
            _append_warning(
                warnings,
                "Remote executor reported duplicate tool names; later duplicates were ignored.",
            )
            continue
        merged.append(tool)
        merged_names.add(tool.name)

    builtin_defs = sorted(
        [tool for tool in agent_tools if tool.source.type in ("builtin", "skill")],
        key=_tool_collision_identity,
    )
    for tool in builtin_defs:
        if tool.name in merged_names:
            collision_count += 1
            _append_warning(
                warnings,
                "Some controller tools were shadowed because the remote executor already exposes the same runtime name.",
            )
            continue
        merged.append(tool)
        merged_names.add(tool.name)

    if intaris_result is None:
        intaris_result = await _resolve_intaris_mcp_tools(
            providers,
            agent,
            disabled_categories,
            disabled_tools,
        )
    for warning in intaris_result.warnings:
        _append_warning(warnings, warning)
    collision_count += intaris_result.collision_count
    for tool in intaris_result.tools:
        if tool.name in merged_names:
            collision_count += 1
            INTARIS_MCP_COLLISIONS.inc()
            _append_warning(
                warnings,
                "Some Intaris MCP tools were hidden because another runtime tool already uses the same name.",
            )
            continue
        merged.append(tool)
        merged_names.add(tool.name)

    return RemoteInventoryMergeResult(
        tools=merged,
        warnings=warnings,
        collision_count=collision_count,
        intaris_fallback_used=intaris_result.fallback_used,
    )


def _append_warning(warnings: list[str], warning: str) -> None:
    """Append a stable warning string once."""
    if warning not in warnings:
        warnings.append(warning)


def _tool_collision_identity(tool: ToolDefinition) -> str:
    """Return the runtime-visible identity used for merge deduplication."""
    return tool.name


def _extract_intaris_aggregated_server_name(row: dict[str, Any]) -> str | None:
    """Extract the canonical Intaris server name from an aggregated row."""
    return extract_intaris_aggregated_server_name(row)


def _extract_intaris_aggregated_raw_tool_name(row: dict[str, Any]) -> str | None:
    """Extract the canonical raw Intaris MCP tool name from an aggregated row."""
    return extract_intaris_aggregated_raw_tool_name(row)


def _build_intaris_tool_definition(
    *,
    server_name: str,
    raw_tool_name: str,
    payload: dict[str, Any],
) -> ToolDefinition:
    return build_intaris_tool_definition(
        server_name=server_name,
        raw_tool_name=raw_tool_name,
        payload=payload,
    )


async def _resolve_intaris_mcp_tools_from_server_cache(
    providers: Any,
    *,
    server_names: set[str],
    disabled_categories: set[str],
    disabled_tools: set[str],
) -> IntarisMCPResolutionResult:
    result = IntarisMCPResolutionResult()
    if not server_names:
        return result
    try:
        all_servers = await providers.guardrails.list_mcp_servers(enabled_only=True)
    except Exception:
        INTARIS_MCP_SERVER_LIST_FAILURES.inc()
        logger.warning("Failed to list Intaris MCP servers", exc_info=True)
        _append_warning(result.warnings, "Unable to load fallback Intaris MCP server manifests.")
        return result

    by_server: dict[str, list[ToolDefinition]] = {name: [] for name in server_names}
    for server in all_servers:
        if not isinstance(server, dict):
            continue
        name = server.get("name")
        if not isinstance(name, str) or name not in server_names:
            continue
        tools_cache = server.get("tools_cache") or []
        if not isinstance(tools_cache, list):
            continue
        for raw_tool in tools_cache:
            if not isinstance(raw_tool, dict):
                continue
            raw_tool_name = raw_tool.get("name")
            if not isinstance(raw_tool_name, str) or not raw_tool_name:
                continue
            tool = _build_intaris_tool_definition(
                server_name=name,
                raw_tool_name=raw_tool_name,
                payload=raw_tool,
            )
            if "mcp" in disabled_categories:
                continue
            if tool.name in disabled_tools or stable_tool_id(tool) in disabled_tools:
                continue
            by_server[name].append(tool)

    for server_name in sorted(by_server):
        deduped = _dedupe_intaris_tools(by_server[server_name], result.warnings)
        result.collision_count += len(by_server[server_name]) - len(deduped)
        result.tools.extend(deduped)
    if result.tools:
        result.fallback_used = True
        INTARIS_MCP_FALLBACKS.inc(len(server_names))
        _append_warning(
            result.warnings,
            "Fell back to cached Intaris MCP server manifests for some assigned servers.",
        )
    return result


def _dedupe_intaris_tools(
    tools: list[ToolDefinition],
    warnings: list[str],
) -> list[ToolDefinition]:
    deduped: list[ToolDefinition] = []
    seen: set[str] = set()
    for tool in sorted(
        tools,
        key=lambda item: (
            item.source.server_name or "",
            item.source.raw_tool_name or item.name,
            item.name,
        ),
    ):
        identity = _tool_collision_identity(tool)
        if identity in seen:
            INTARIS_MCP_COLLISIONS.inc()
            _append_warning(
                warnings,
                "Some Intaris MCP tools were skipped because multiple tools resolved to the same runtime name.",
            )
            continue
        seen.add(identity)
        deduped.append(tool)
    return deduped


async def _resolve_intaris_mcp_tools(
    providers: Any,
    agent: AgentDefinition,
    disabled_categories: set[str],
    disabled_tools: set[str],
) -> IntarisMCPResolutionResult:
    """Resolve Intaris MCP tools assigned to an agent.

    Uses the aggregated Intaris MCP tool listing as the primary source of
    truth. If assigned servers cannot be reconstructed from aggregated rows
    because canonical source metadata is missing, falls back to the server
    cache manifest for only those affected servers.
    """
    if not isinstance(agent.tools, dict):
        return IntarisMCPResolutionResult()
    server_names = agent.tools.get("intaris_mcp_servers", [])
    if not isinstance(server_names, list) or not server_names:
        return IntarisMCPResolutionResult()

    allowed_names = set(server_names)
    guardrails = getattr(providers, "guardrails", None)
    if guardrails is None:
        return IntarisMCPResolutionResult()

    start = perf_counter()
    try:
        aggregated_tools = await guardrails.list_mcp_tools()
    except Exception:
        INTARIS_MCP_LIST_FAILURES.inc()
        logger.warning("Failed to list Intaris MCP tools", exc_info=True)
        result = await _resolve_intaris_mcp_tools_from_server_cache(
            providers,
            server_names=allowed_names,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools,
        )
        _append_warning(result.warnings, "Unable to load aggregated Intaris MCP tools.")
        INTARIS_MCP_RESOLUTION_LATENCY.observe(perf_counter() - start)
        return result

    result = IntarisMCPResolutionResult()
    if not isinstance(aggregated_tools, list):
        aggregated_tools = []

    if not aggregated_tools:
        fallback = await _resolve_intaris_mcp_tools_from_server_cache(
            providers,
            server_names=allowed_names,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools,
        )
        if fallback.tools:
            _append_warning(
                fallback.warnings,
                "Aggregated Intaris MCP listing was empty; used cached server manifests where available.",
            )
            INTARIS_MCP_RESOLUTION_LATENCY.observe(perf_counter() - start)
            return fallback
        _append_warning(
            fallback.warnings,
            "Aggregated Intaris MCP listing was empty and no cached server manifests provided replacement tools.",
        )
        INTARIS_MCP_RESOLUTION_LATENCY.observe(perf_counter() - start)
        return fallback

    by_server: dict[str, list[ToolDefinition]] = {name: [] for name in allowed_names}
    malformed_servers: set[str] = set()
    seen_servers: set[str] = set()
    for row in aggregated_tools:
        if not isinstance(row, dict):
            INTARIS_MCP_SKIPPED_ROWS.inc()
            continue
        server_name = _extract_intaris_aggregated_server_name(row)
        raw_tool_name = _extract_intaris_aggregated_raw_tool_name(row)
        if server_name in allowed_names:
            seen_servers.add(server_name)
        if server_name in allowed_names and not raw_tool_name:
            malformed_servers.add(server_name)
        if not server_name or not raw_tool_name:
            INTARIS_MCP_SKIPPED_ROWS.inc()
            continue
        if server_name not in allowed_names:
            continue
        tool = _build_intaris_tool_definition(
            server_name=server_name,
            raw_tool_name=raw_tool_name,
            payload=row,
        )
        if "mcp" in disabled_categories:
            continue
        if tool.name in disabled_tools or stable_tool_id(tool) in disabled_tools:
            continue
        by_server[server_name].append(tool)

    unresolved_servers = {
        name
        for name in allowed_names
        if name in malformed_servers or (not by_server.get(name) and name not in seen_servers)
    }
    if unresolved_servers:
        fallback = await _resolve_intaris_mcp_tools_from_server_cache(
            providers,
            server_names=unresolved_servers,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools,
        )
        result.fallback_used = fallback.fallback_used
        result.collision_count += fallback.collision_count
        result.warnings.extend(fallback.warnings)
        for tool in fallback.tools:
            server_name = tool.source.server_name or ""
            if server_name in unresolved_servers:
                by_server.setdefault(server_name, []).append(tool)
        if fallback.fallback_used:
            _append_warning(
                result.warnings,
                "Some assigned Intaris MCP servers required cache fallback because aggregated tool metadata was incomplete.",
            )
        else:
            _append_warning(
                result.warnings,
                "Some assigned Intaris MCP servers could not be reconstructed from aggregated metadata and had no cached manifest fallback.",
            )

    for server_name in sorted(by_server):
        deduped = _dedupe_intaris_tools(by_server[server_name], result.warnings)
        result.collision_count += len(by_server[server_name]) - len(deduped)
        result.tools.extend(deduped)

    if result.tools:
        logger.debug(
            "Resolved Intaris MCP tools",
            extra={
                "extra_data": {
                    "count": len(result.tools),
                    "servers": sorted(allowed_names),
                    "fallback_used": result.fallback_used,
                    "collision_count": result.collision_count,
                }
            },
        )
    INTARIS_MCP_RESOLUTION_LATENCY.observe(perf_counter() - start)
    return result
