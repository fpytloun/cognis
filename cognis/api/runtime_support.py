"""Helpers for building API/runtime tool execution context."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

from prometheus_client import Counter, Histogram

from cognis.api.tool_inventory import (
    build_intaris_tool_definition,
    extract_intaris_aggregated_raw_tool_name,
    extract_intaris_aggregated_server_name,
)
from cognis.core.executor_pin_lifecycle import (
    ensure_active_executor_pin,
    load_executor_pin_lifecycle_settings,
)
from cognis.core.executor_policy import (
    ExecutorPolicy,
    is_executor_row_usable,
    is_executor_type_allowed,
    load_executor_policy,
)
from cognis.core.mcp_oauth import MCPOAuthError, oauth_required_mcp_status
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
    ToolSource,
    effective_mcp_auth_config,
    tool_matches_identifier,
)
from cognis.ownership import SYSTEM_USER_EMAIL, normalize_executor_scope
from cognis.providers.base import ImageGenerationProvider
from cognis.runtime_context import (
    RuntimeAccessContext,
    current_effective_working_directory,
    current_workspace_root,
)
from cognis.tools.builtin.agent_management import agent_management_tools
from cognis.tools.builtin.artifact_tools import artifact_tools
from cognis.tools.builtin.conversations import (
    build_conversation_tool_handlers,
    conversation_tools,
)
from cognis.tools.builtin.datetime_tools import build_datetime_tool_handlers, datetime_tools
from cognis.tools.builtin.image import image_tools
from cognis.tools.builtin.knowledgebase import (
    build_knowledgebase_tool_handlers,
    knowledgebase_tools,
)
from cognis.tools.builtin.memory import memory_tools
from cognis.tools.builtin.orchestration import orchestration_tools
from cognis.tools.builtin.projects import build_project_tool_handlers, project_tools
from cognis.tools.builtin.skill_management import (
    materialize_loaded_skill_context,
    skill_management_tools,
)
from cognis.tools.builtin.system import build_system_tool_handlers, system_tools
from cognis.tools.builtin.task_continuation import (
    build_task_continuation_tool_handlers,
    task_continuation_tools,
)
from cognis.tools.builtin.tool_output import tool_output_tools
from cognis.tools.builtin.workflow import workflow_tools
from cognis.tools.executor.definitions import executor_tool_definitions, executor_tool_handlers
from cognis.tools.mcp import disambiguate_mcp_tool_name_collisions, invalid_mcp_config_reason
from cognis.tools.registry import RegisteredTool, ToolRegistry
from cognis.tools.skills import (
    attached_skill_tool_ids,
    attached_skill_tool_ids_by_skill,
    build_available_skills_metadata,
    discoverable_skill_tools_to_definitions,
    load_skill_tool_names,
    resolve_skills_for_agent,
)

logger = get_logger(__name__)

MCP_TOOL_SOURCE_TYPES = frozenset({"local_mcp", "intaris_mcp"})


def mcp_server_assignment_key(source: ToolSource) -> str | None:
    """Return the stable per-agent assignment key for an MCP tool source."""

    if source.type not in MCP_TOOL_SOURCE_TYPES:
        return None
    server_key = source.server_id or source.server_name
    if not server_key:
        return None
    return f"{source.type}:{server_key}"


def disabled_mcp_server_keys(agent_tools_config: dict[str, Any]) -> set[str]:
    """Extract normalized disabled MCP server assignment keys from agent tools config."""

    raw_keys = agent_tools_config.get("disabled_mcp_servers") or []
    if not isinstance(raw_keys, list):
        return set()
    return {str(item) for item in raw_keys if isinstance(item, str) and item.strip()}


def tool_disabled_by_agent_config(
    tool: ToolDefinition,
    *,
    disabled_categories: set[str],
    disabled_tools: set[str],
    disabled_mcp_servers: set[str],
) -> bool:
    """Return whether an agent-level tool disable rule hides this tool."""

    if tool.category in disabled_categories:
        return True
    if any(tool_matches_identifier(tool, identifier) for identifier in disabled_tools):
        return True
    server_key = mcp_server_assignment_key(tool.source)
    return bool(server_key and server_key in disabled_mcp_servers)


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


DEFAULT_OFF_BUILTIN_TOOLS = frozenset({"manage_agents"})

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

RuntimeFactory = Callable[..., Awaitable[ResolvedStepRuntime]]


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


def _agent_executor_binding(agent: AgentDefinition) -> tuple[str, bool]:
    execution = agent.execution if isinstance(agent.execution, dict) else {}
    if execution.get("executor_id"):
        return "explicit", True
    selector = execution.get("executor_selector")
    if isinstance(selector, dict) and selector:
        return "selector", True
    if selector is not None:
        return "empty_selector", False
    return "none", False


def _agent_executor_binding_from_execution(execution: dict[str, Any]) -> tuple[str, bool]:
    if execution.get("executor_id"):
        return "explicit", True
    selector = execution.get("executor_selector")
    if isinstance(selector, dict) and selector:
        return "selector", True
    if selector is not None:
        return "empty_selector", False
    return "none", False


def _grant_execution(grant: Any | None) -> dict[str, Any]:
    overrides = getattr(grant, "grantee_overrides", None)
    if not isinstance(overrides, dict):
        return {}
    execution = overrides.get("execution")
    return execution if isinstance(execution, dict) else {}


def _environment_payload(snapshot: Any | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "available": bool(getattr(snapshot, "available", False)),
        "executor_id": getattr(snapshot, "executor_id", None),
        "executor_type": getattr(snapshot, "executor_type", None),
        "user": getattr(snapshot, "user", None),
        "home": getattr(snapshot, "home", None),
        "cwd": getattr(snapshot, "cwd", None),
        "hostname": getattr(snapshot, "hostname", None),
        "platform_os": getattr(snapshot, "platform_os", None),
        "platform_arch": getattr(snapshot, "platform_arch", None),
        "source": getattr(snapshot, "source", None),
        "observed_at": getattr(snapshot, "observed_at", None),
    }


def _runtime_info(
    *,
    executor_config: dict[str, Any] | None,
    selection_source: str,
    hard_bound: bool,
    runtime_source: str,
    fallback_used: bool,
    environment: Any | None,
    inventory_tool_count: int,
    visible_tool_count: int | None = None,
    failure_reason: str | None = None,
    executor_pin_fallback_notice: dict[str, Any] | None = None,
    tool_agent: AgentDefinition | None = None,
    executor_agent: AgentDefinition | None = None,
) -> dict[str, Any]:
    return {
        "strict_executor": True,
        "tool_agent_id": tool_agent.agent_id if tool_agent else None,
        "tool_agent_type": tool_agent.agent_type if tool_agent else None,
        "tool_agent_owner_email": tool_agent.owner_email if tool_agent else None,
        "executor_agent_id": executor_agent.agent_id if executor_agent else None,
        "executor_agent_type": executor_agent.agent_type if executor_agent else None,
        "executor_agent_owner_email": executor_agent.owner_email if executor_agent else None,
        "executor_id": executor_config.get("executor_id") if executor_config else None,
        "executor_type": executor_config.get("executor_type") if executor_config else None,
        "executor_owner_email": executor_config.get("executor_owner_email")
        if executor_config
        else None,
        "selected_executor_owner_email": executor_config.get("owner_email")
        if executor_config
        else None,
        "selection_source": selection_source,
        "hard_bound": hard_bound,
        "runtime_source": runtime_source,
        "fallback_used": fallback_used,
        "failure_reason": failure_reason,
        "executor_pin_fallback_notice": executor_pin_fallback_notice,
        "environment": _environment_payload(environment),
        "inventory_tool_count": inventory_tool_count,
        "visible_tool_count": visible_tool_count,
    }


def _raise_runtime_resolution_error(
    message: str,
    *,
    executor_config: dict[str, Any] | None,
    selection_source: str,
    hard_bound: bool,
) -> None:
    logger.warning(
        "executor runtime resolution failed",
        extra={
            "extra_data": {
                "executor_id": executor_config.get("executor_id") if executor_config else None,
                "executor_type": executor_config.get("executor_type") if executor_config else None,
                "selection_source": selection_source,
                "hard_bound": hard_bound,
                "reason": message,
            }
        },
    )
    raise RuntimeError(message)


def _executor_config_from_row(
    row: Any,
    *,
    executor_owner_email: str,
    selection_source: str,
) -> dict[str, Any]:
    return {
        "executor_id": row.executor_id,
        "executor_type": row.executor_type,
        "enabled_tools": row.enabled_tools or [],
        "enabled_tool_groups": row.enabled_tool_groups or [],
        "labels": row.labels or {},
        "config": row.config or {},
        "owner_email": row.owner_email,
        "executor_owner_email": executor_owner_email,
        "selection_source": selection_source,
        "desired_config_version": row.desired_config_version,
        "applied_config_version": row.applied_config_version,
        "observed_tools": row.observed_tools or [],
        "last_observed_at": row.last_observed_at,
        "runtime_state": row.runtime_state,
    }


async def _resolve_eligible_executor_config(
    providers: Any,
    agent: AgentDefinition,
    user_email: str,
    policy: ExecutorPolicy,
    *,
    conversation_active_executor_id: str | None = None,
    conversation_active_executor_expires_at: Any | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Resolve the only executor eligible for an agent execution.

    Stage 36 multi-executor agents:

    - Primary executors (``execution.executor_id`` or
      ``execution.executor_selector``) are the only auto-eligible binding.
    - When ``conversation_active_executor_id`` is set, it pins the runtime
      to that executor (provided it is currently assigned to the agent and
      usable). The pin is the conversation's persisted choice from a prior
      ``switch_executor`` or ``/executor``.
    - When a non-primary pin expires or the executor disconnects, the
      controller switches the pin back to a usable primary executor and
      returns a factual notice for the UI and LLM context.
    - When no pin is set and the agent is on its first turn, the
      controller picks one usable primary executor (preferring
      ``runtime_state == active`` over ``degraded``, then sorted by id)
      and persists it via ``initialize_conversation_active_executor``.
      This is the one and only initial pick — the controller never
      re-picks afterwards.
    - A primary selector matching N>=1 usable executors is supported (no
      longer raises on multi-match) — the initial pick chooses one.
    """

    session_factory = getattr(providers, "_session_factory", None)
    if session_factory is None:
        raise RuntimeError("Session factory unavailable; cannot resolve executor")

    from cognis.core.executor_pool import pick_initial_active, resolve_executor_pool
    from cognis.store.queries import (
        get_active_agent_grant,
        get_executor_row,
        initialize_conversation_active_executor,
        initialize_task_active_executor,
        list_executors,
    )

    async def _persist_initial_active_executor(session: Any, executor_id: str) -> None:
        """Idempotently persist the initial active executor on conversation+task."""

        try:
            if isinstance(conversation_id, str) and conversation_id:
                await initialize_conversation_active_executor(session, conversation_id, executor_id)
            if isinstance(task_id, str) and task_id:
                await initialize_task_active_executor(session, task_id, executor_id)
            commit = getattr(session, "commit", None)
            if callable(commit):
                await commit()
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                with contextlib.suppress(Exception):
                    await rollback()
            logger.debug(
                "stage36: failed to persist initial active executor",
                exc_info=True,
            )

    executor_owner_email = user_email
    execution = agent.execution if isinstance(agent.execution, dict) else {}
    allow_default = False

    async with session_factory() as session:
        if user_email != agent.owner_email:
            grant = await get_active_agent_grant(session, agent.agent_id, user_email)
            if grant is not None:
                executor_scope = normalize_executor_scope(str(getattr(grant, "executor_scope", "")))
                if executor_scope == "owner_executor":
                    executor_owner_email = agent.owner_email
                else:
                    execution = _grant_execution(grant)
                    allow_default = True

        explicit_id = execution.get("executor_id")
        selector = execution.get("executor_selector")
        selection_source, _hard_bound = _agent_executor_binding_from_execution(execution)
        if (
            not explicit_id
            and selector is not None
            and not (isinstance(selector, dict) and selector)
        ):
            raise RuntimeError("Agent executor_selector must be a non-empty object")

        # Stage 36: conversation-level active executor pin overrides primary
        # binding. The pin must point at an executor that is currently
        # ASSIGNED to the agent (primary or additional) AND usable. If not,
        # we raise so the tool dispatch path returns a factual error — the
        # controller never silently re-routes.
        if isinstance(conversation_active_executor_id, str) and conversation_active_executor_id:
            pool = await resolve_executor_pool(
                session_factory=session_factory,
                agent_execution=execution,
                user_email=user_email,
                executor_owner_email=executor_owner_email,
                policy=policy,
            )
            lifecycle = None
            if conversation_id or task_id:
                settings = await load_executor_pin_lifecycle_settings(session_factory)
                lifecycle = await ensure_active_executor_pin(
                    session_factory=session_factory,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    pool=pool,
                    active_executor_id=conversation_active_executor_id,
                    active_executor_expires_at=conversation_active_executor_expires_at,
                    ws_provider=getattr(getattr(providers, "executor", None), "websocket", None),
                    retry_seconds=settings["retry_seconds"],
                    retry_interval_seconds=settings["retry_interval_seconds"],
                )
                if lifecycle.active_executor_id:
                    conversation_active_executor_id = lifecycle.active_executor_id
            assert isinstance(conversation_active_executor_id, str)
            target = pool.by_id(conversation_active_executor_id)
            if target is None:
                raise RuntimeError(
                    f"Conversation-active executor '{conversation_active_executor_id}' "
                    "is no longer assigned to this agent. Use switch_executor "
                    "or /executor to choose a usable executor."
                )
            if target.row is None or not is_executor_row_usable(
                target.row, policy, owner_email=executor_owner_email
            ):
                raise RuntimeError(
                    f"Conversation-active executor '{conversation_active_executor_id}' "
                    f"is not usable (state: {target.state.value})"
                )
            # Backfill: if the conversation pin came from a pre-existing
            # source but the task pin is still NULL (e.g. mid-deploy
            # upgrade), seed the task pin too. The IS NULL guards keep
            # this idempotent.
            await _persist_initial_active_executor(session, target.executor_id)
            config = _executor_config_from_row(
                target.row,
                executor_owner_email=executor_owner_email,
                selection_source=(
                    "conversation_active_primary"
                    if target.is_primary
                    else "conversation_active_additional"
                ),
            )
            notice = getattr(lifecycle, "notice", None)
            if notice is not None:
                config["executor_pin_fallback_notice"] = {
                    "previous_executor_id": notice.previous_executor_id,
                    "new_executor_id": notice.new_executor_id,
                    "reason": notice.reason,
                    "ui_message": notice.ui_message,
                    "llm_message": notice.llm_message,
                }
            return config

        if explicit_id:
            # Explicit primary id is a single-row lookup — keep the legacy
            # path (no pool resolution needed).
            row = await get_executor_row(
                session,
                str(explicit_id),
                owner_email=executor_owner_email,
                include_shared=True,
            )
            if row is None:
                raise RuntimeError(f"Executor '{explicit_id}' is not available to this user")
            if not is_executor_row_usable(row, policy, owner_email=executor_owner_email):
                raise RuntimeError(f"Executor '{explicit_id}' is not active or allowed by policy")
            # Persist as the conversation+task's initial active executor on first turn.
            await _persist_initial_active_executor(session, str(explicit_id))
            return _executor_config_from_row(
                row,
                executor_owner_email=executor_owner_email,
                selection_source="explicit",
            )

        if not explicit_id and not selector:
            if not allow_default:
                raise RuntimeError(
                    "Agent must explicitly configure executor_id or executor_selector"
                )
            candidates = await list_executors(
                session, owner_email=executor_owner_email, include_shared=True
            )
            default_matches = [
                row
                for row in candidates
                if bool(getattr(row, "is_default", False))
                and is_executor_row_usable(row, policy, owner_email=executor_owner_email)
            ]
            private_defaults = [
                row
                for row in default_matches
                if getattr(row, "owner_email", None) == executor_owner_email
            ]
            selected = (
                private_defaults[0]
                if private_defaults
                else (default_matches[0] if default_matches else None)
            )
            if selected is None:
                raise RuntimeError(
                    "No default executor is configured for this shared agent. Configure your executor on the agent page."
                )
            return _executor_config_from_row(
                selected,
                executor_owner_email=executor_owner_email,
                selection_source="default",
            )

        # Stage 36: selector path. A primary selector matching N>=1 usable
        # executors yields a primary set of size N; the controller picks
        # one (preferring runtime_state==active over degraded, then sorted
        # by id) and persists it as the conversation's initial active
        # executor.
        assert isinstance(selector, dict)
        pool = await resolve_executor_pool(
            session_factory=session_factory,
            agent_execution=execution,
            user_email=user_email,
            executor_owner_email=executor_owner_email,
            policy=policy,
        )
        initial = pick_initial_active(pool)
        if initial is None or initial.row is None:
            usable_count = len(pool.usable_primaries())
            raise RuntimeError(
                f"Executor selector for agent '{agent.agent_id}' matched {usable_count} usable executors"
            )
        await _persist_initial_active_executor(session, initial.executor_id)
        return _executor_config_from_row(
            initial.row,
            executor_owner_email=executor_owner_email,
            selection_source=selection_source,
        )


def static_tool_definitions(*, knowledgebase_enabled: bool = False) -> list[ToolDefinition]:
    """Return all static tool definitions available to Cognis.

    Includes builtin controller tools, workflow tools, memory tools,
    executor-native tools, and a maximal set of web tools (for
    discovery/listing — actual web tools sent to the LLM are filtered
    per-session based on available backends).
    """
    from cognis.tools.executor.web.definitions import web_tool_definitions

    # Include all web tools for discovery (as if all backends were available)
    all_web = web_tool_definitions(["direct", "tavily", "brave"], default_backend="direct")
    from cognis.tools.builtin.schedule import schedule_tools
    from cognis.tools.executor.definitions import OFFICE_EXECUTOR_TOOLS

    return [
        *system_tools(),
        *artifact_tools(),
        *datetime_tools(),
        *orchestration_tools(),
        *workflow_tools(),
        *memory_tools(),
        *agent_management_tools(),
        *conversation_tools(),
        *project_tools(),
        *(knowledgebase_tools() if knowledgebase_enabled else []),
        *task_continuation_tools(),
        *tool_output_tools(),
        *image_tools(),
        *skill_management_tools(),
        *schedule_tools(),
        *executor_tool_definitions(),
        *OFFICE_EXECUTOR_TOOLS,
        *all_web,
    ]


def _opted_in_builtin_tools(agent: Any | None) -> set[str]:
    if agent is None or not isinstance(getattr(agent, "tools", None), dict):
        return set()
    raw = agent.tools.get("opt_in_builtin_tools")
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if isinstance(item, str) and item.strip()}


def _management_tools_allowed(
    agent: Any | None,
    access_context: RuntimeAccessContext | None,
) -> bool:
    if agent is None:
        return False
    opted_in = _opted_in_builtin_tools(agent)
    agent_tools_config = agent.tools if isinstance(getattr(agent, "tools", None), dict) else {}
    explicit_allow = {
        str(item) for item in agent_tools_config.get("allow_tools") or [] if isinstance(item, str)
    }
    if (
        "manage_agents" not in opted_in
        and "manage_agents" not in explicit_allow
        and "builtin:manage_agents" not in explicit_allow
    ):
        return False
    if getattr(agent, "agent_type", "primary") == "secondary":
        return False
    return bool(access_context is not None and access_context.is_root_owner_primary_chat)


def select_static_tools(
    agent: Any | None = None,
    *,
    access_context: RuntimeAccessContext | None = None,
    knowledgebase_enabled: bool = False,
) -> list[ToolDefinition]:
    """Filter static builtin tools for an agent definition."""
    definitions = static_tool_definitions(knowledgebase_enabled=knowledgebase_enabled)
    if agent is None:
        return [tool for tool in definitions if tool.name not in DEFAULT_OFF_BUILTIN_TOOLS]

    agent_tools_config = agent.tools if isinstance(agent.tools, dict) else {}
    builtin_allow = agent_tools_config.get("builtin_tools")
    allowlist = builtin_allow if isinstance(builtin_allow, list) else None
    explicit_allow = {
        str(item) for item in agent_tools_config.get("allow_tools") or [] if isinstance(item, str)
    }
    explicit_deny = {
        str(item) for item in agent_tools_config.get("deny_tools") or [] if isinstance(item, str)
    }
    configured_groups = {
        str(item) for item in agent_tools_config.get("tool_groups") or [] if isinstance(item, str)
    }
    group_allow: set[str] = set()
    if configured_groups:
        from cognis.core.agent_management import TOOL_GROUP_DEFINITIONS

        group_allow = {
            tool_id
            for group in TOOL_GROUP_DEFINITIONS
            if group.group_id in configured_groups
            for tool_id in group.tool_ids
        }
    skill_tool_names = load_skill_tool_names(agent)
    allow_all_builtins = allowlist is None or "*" in allowlist
    delegation_enabled = bool(agent_tools_config.get("delegation_tools", True))

    disabled_categories = set(agent_tools_config.get("disabled_categories") or [])
    disabled_tools = set(agent_tools_config.get("disabled_tools") or [])
    disabled_mcp_servers = disabled_mcp_server_keys(agent_tools_config)
    skill_mutation_tools = {
        "skill_write",
        "skill_delete",
        "skill_import_url",
        "skill_restore_version",
        "skill_asset_write",
        "skill_asset_delete",
    }
    allow_skill_mutations = getattr(agent, "agent_type", "primary") != "secondary"

    selected: list[ToolDefinition] = []
    for tool in definitions:
        default_off_allowed = False
        if tool.name in DEFAULT_OFF_BUILTIN_TOOLS:
            default_off_allowed = _management_tools_allowed(agent, access_context)
            if not default_off_allowed:
                continue
        # Agent-level disable takes precedence
        if any(tool_matches_identifier(tool, identifier) for identifier in explicit_deny):
            continue
        if tool_disabled_by_agent_config(
            tool,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools,
            disabled_mcp_servers=disabled_mcp_servers,
        ):
            continue
        if default_off_allowed:
            selected.append(tool)
            continue
        if tool.category == "orchestration":
            if delegation_enabled:
                selected.append(tool)
            continue
        if not allow_skill_mutations and tool.name in skill_mutation_tools:
            continue
        if (
            allow_all_builtins
            or (
                allowlist is not None
                and (
                    tool.name in allowlist
                    or any(tool_matches_identifier(tool, identifier) for identifier in allowlist)
                )
            )
            or any(tool_matches_identifier(tool, identifier) for identifier in group_allow)
            or any(tool_matches_identifier(tool, identifier) for identifier in explicit_allow)
            or tool.name in skill_tool_names
        ):
            selected.append(tool)
    return selected


async def enrich_image_tool_model_descriptions(
    tools: list[ToolDefinition],
    image_generation_provider: ImageGenerationProvider | None,
) -> list[ToolDefinition]:
    """Annotate image tools with configured image-generation models."""

    if image_generation_provider is None:
        return tools
    try:
        default_model = await image_generation_provider.resolve_model(task_type="image_generation")  # type: ignore[attr-defined]
        models = await image_generation_provider.list_models()  # type: ignore[attr-defined]
    except Exception:
        logger.debug("Failed to enrich image tool model descriptions", exc_info=True)
        return tools

    image_models = sorted(
        {
            str(model.get("model_id"))
            for model in models
            if isinstance(model, dict)
            and isinstance(model.get("model_id"), str)
            and (
                model.get("supports_image_generation") is True
                or _looks_like_image_model_id(str(model.get("model_id")))
            )
        }
    )
    if default_model and default_model not in image_models:
        image_models.insert(0, default_model)
    if not image_models and not default_model:
        return tools

    model_description = _image_model_parameter_description(
        default_model=default_model,
        image_models=image_models,
    )
    enriched: list[ToolDefinition] = []
    for tool in tools:
        if tool.name not in {"image_generate", "image_edit"}:
            enriched.append(tool)
            continue
        parameters = dict(tool.parameters or {})
        properties = dict(parameters.get("properties") or {})
        model_property = dict(properties.get("model") or {})
        model_property["description"] = model_description
        if image_models:
            model_property["enum"] = image_models
        properties["model"] = model_property
        parameters["properties"] = properties
        enriched.append(tool.model_copy(update={"parameters": parameters}))
    return enriched


def _looks_like_image_model_id(model_id: str) -> bool:
    normalized = model_id.lower().replace("_", "-")
    return any(
        token in normalized
        for token in ("gpt-image", "dall-e", "image-generation", "imagen", "image-preview")
    )


def _image_model_parameter_description(
    *,
    default_model: str | None,
    image_models: list[str],
) -> str:
    if image_models:
        allowed = ", ".join(image_models)
        if default_model:
            return (
                f"Optional image-generation model. Default: {default_model}. "
                f"Allowed configured models: {allowed}. Omit this unless the user asks "
                "for a specific model."
            )
        return f"Optional image-generation model. Allowed configured models: {allowed}."
    if default_model:
        return (
            f"Optional image-generation model. Default: {default_model}. "
            "Omit this unless the user asks for a specific model."
        )
    return "Optional image-generation model. Omit to use the configured default."


def _build_handler_map(
    session_factory: Any,
    status_provider: Any,
    guardrails_provider: Any | None = None,
    compaction_strategy: Any | None = None,
    knowledgebase_service: Any | None = None,
) -> dict[str, Any]:
    """Build a combined handler map for all tool sources."""
    handlers: dict[str, Any] = {}
    handlers.update(build_system_tool_handlers(session_factory, status_provider))
    if guardrails_provider is not None:
        handlers.update(
            build_conversation_tool_handlers(
                session_factory,
                guardrails_provider,
                compaction_strategy,
            )
        )
    handlers.update(build_project_tool_handlers(session_factory))
    if knowledgebase_service is not None:
        handlers.update(build_knowledgebase_tool_handlers(knowledgebase_service))
    handlers.update(build_task_continuation_tool_handlers(session_factory))
    handlers.update(build_datetime_tool_handlers())
    handlers.update(executor_tool_handlers())
    return handlers


def build_registry_with_handlers(
    tools: list[ToolDefinition],
    handler_map: dict[str, Any],
    *,
    artifact_store: Any | None = None,
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

            handler = _build_skill_handler(tool, artifact_store=artifact_store)
        registry.register(RegisteredTool(definition=tool, handler=cast(Any, handler)))
    return registry


def _build_remote_runtime_registry(
    tools: list[ToolDefinition],
    handler_map: dict[str, Any],
) -> ToolRegistry:
    """Build a remote runtime registry.

    Remote runtimes expose executor-native tools over RPC, but controller-side
    builtins still need local handlers so the router can execute them locally.
    """

    registry = ToolRegistry()
    for tool in tools:
        handler = handler_map.get(tool.name) if tool.source.type == "builtin" else None
        registry.register(RegisteredTool(definition=tool, handler=cast(Any, handler)))
    return registry


def build_static_registry(
    agent: AgentDefinition | None = None, *, knowledgebase_enabled: bool = False
) -> ToolRegistry:
    """Build a static ToolRegistry for one agent's builtin tools.

    NOTE: This registry has handler=None for all tools. It is used only for
    tool listing/discovery, NOT for execution. For execution, use
    build_registry_with_handlers().
    """
    registry = ToolRegistry()
    for tool in select_static_tools(agent, knowledgebase_enabled=knowledgebase_enabled):
        registry.register(RegisteredTool(definition=tool))
    return registry


async def build_shared_runtime(
    providers: Any,
    *,
    knowledgebase_enabled: bool = False,
) -> ResolvedStepRuntime:
    """Build the shared builtin runtime used as the template for step runtimes."""
    session_factory = getattr(providers, "_session_factory", None)
    if session_factory is not None:
        policy = await load_executor_policy(session_factory)
        if not policy.allow_in_process:
            logger.info("Shared in-process executor disabled by policy; using static-only template")
            return ResolvedStepRuntime(
                tool_registry=build_static_registry(knowledgebase_enabled=knowledgebase_enabled),
                executor_connection=None,
                cleanup=noop_cleanup,
                executor_environment=build_local_executor_environment(
                    executor_type="in_process",
                    source="shared_runtime_disabled",
                ),
            )
    tools = static_tool_definitions(knowledgebase_enabled=knowledgebase_enabled)
    handle = await providers.executor.spawn(
        ExecutorConfig(
            executor_id="controller_shared_builtin",
            tools=tools,
            metadata={"executor_type": "in_process"},
        )
    )
    connection = await providers.executor.get_executor(handle)
    registry = build_static_registry(knowledgebase_enabled=knowledgebase_enabled)

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
    artifact_store: Any | None = None,
    knowledgebase_service: Any | None = None,
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
        *,
        executor_agent: AgentDefinition | None = None,
        access_context: RuntimeAccessContext | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        _executor_pin_fallback_notice: dict[str, Any] | None = None,
        _executor_pin_fallback_retried: bool = False,
    ) -> ResolvedStepRuntime:
        tool_agent = agent
        executor_agent = executor_agent or agent
        session_factory_for_policy = getattr(providers, "_session_factory", None) or session_factory
        policy = (
            await load_executor_policy(session_factory_for_policy)
            if session_factory_for_policy is not None
            else None
        )
        if policy is None:
            raise RuntimeError("Executor policy unavailable; refusing to run agent tools")
        selection_source, hard_bound_executor = _agent_executor_binding(executor_agent)

        # Stage 36: read conversation-level active_executor_id (if any) to
        # pin the runtime to a previously-switched executor. For task-driven
        # workflows, also fall back to the task-level pin so all steps of a
        # task run on the same executor (workflow engine seeds the step
        # conversation from the task pin, but a freshly-created task pin
        # may not yet be reflected on the conversation row when the first
        # step resolves).
        conversation_active_executor_id: str | None = None
        conversation_active_executor_expires_at: Any | None = None
        if session_factory is not None and (conversation_id or task_id):
            from cognis.store.queries import get_conversation, get_task

            try:
                async with session_factory() as db_session:
                    if conversation_id:
                        conv_row = await get_conversation(db_session, conversation_id)
                        if conv_row is not None:
                            conversation_active_executor_id = getattr(
                                conv_row, "active_executor_id", None
                            )
                            conversation_active_executor_expires_at = getattr(
                                conv_row, "active_executor_expires_at", None
                            )
                    if conversation_active_executor_id is None and task_id:
                        task_row = await get_task(db_session, task_id)
                        if task_row is not None:
                            conversation_active_executor_id = getattr(
                                task_row, "active_executor_id", None
                            )
                            conversation_active_executor_expires_at = getattr(
                                task_row, "active_executor_expires_at", None
                            )
            except Exception:
                logger.debug(
                    "stage36: failed to read conversation/task active_executor_id",
                    exc_info=True,
                )

        executor_config = await _resolve_eligible_executor_config(
            providers,
            executor_agent,
            user_email,
            policy,
            conversation_active_executor_id=conversation_active_executor_id,
            conversation_active_executor_expires_at=conversation_active_executor_expires_at,
            conversation_id=conversation_id,
            task_id=task_id,
        )
        if _executor_pin_fallback_notice is not None:
            executor_config["executor_pin_fallback_notice"] = _executor_pin_fallback_notice

        # Stage 36: resolve the agent's full executor pool (primary + additional)
        # so downstream code can route per-call to other assigned executors.
        from cognis.core.executor_pool import resolve_executor_pool

        executor_owner_email_for_pool = executor_config.get("executor_owner_email", user_email)
        try:
            executor_pool_obj = await resolve_executor_pool(
                session_factory=session_factory,
                agent_execution=(
                    executor_agent.execution if isinstance(executor_agent.execution, dict) else {}
                ),
                user_email=user_email,
                executor_owner_email=executor_owner_email_for_pool,
                policy=policy,
            )
        except Exception:
            logger.debug("stage36: executor pool resolution failed", exc_info=True)
            executor_pool_obj = None

        enabled_tools = executor_config.get("enabled_tools") if executor_config else None
        enabled_groups = executor_config.get("enabled_tool_groups") if executor_config else None

        # Build runtime metadata: user context + executor DB config (LSP settings, etc.)
        db_config = executor_config.get("config", {})
        runtime_metadata = {
            "user_email": user_email,
            "tool_agent_id": tool_agent.agent_id,
            "executor_agent_id": executor_agent.agent_id,
            **db_config,
        }
        if artifact_store is not None:
            runtime_metadata["artifact_store"] = artifact_store
        workspace_root = current_workspace_root.get()
        working_directory = current_effective_working_directory.get()
        if workspace_root:
            runtime_metadata["workspace_root"] = workspace_root
        if working_directory:
            runtime_metadata["working_directory"] = working_directory

        # Inject web backend config (backend name + API keys)
        web_config = await _resolve_web_config(providers, SYSTEM_USER_EMAIL)
        runtime_metadata.update(web_config)

        # Filter tools by agent config AND executor enablement.
        # Exclude web-category tools — they are injected dynamically below
        # based on available backends.
        agent_tools = [
            t
            for t in select_static_tools(
                tool_agent,
                access_context=access_context,
                knowledgebase_enabled=knowledgebase_service is not None
                and bool(getattr(knowledgebase_service, "enabled", False)),
            )
            if t.category != "web"
        ]
        agent_tools = await enrich_image_tool_model_descriptions(
            agent_tools,
            getattr(providers, "image_generation", None),
        )

        # Add dynamic web tool definitions based on available backends
        from cognis.tools.executor.web.definitions import web_tool_definitions

        dynamic_web_tools = web_tool_definitions(
            web_config["web_available_backends"],
            default_backend=web_config.get("web_backend"),
            available_search_backends=web_config.get("web_available_search_backends"),
            available_fetch_backends=web_config.get("web_available_fetch_backends"),
            default_search_backend=web_config.get("web_search_backend"),
            default_fetch_backend=web_config.get("web_fetch_backend"),
        )
        allowed_web_tool_names = {
            tool.name
            for tool in select_static_tools(tool_agent, access_context=access_context)
            if tool.category == "web"
        }
        agent_tools.extend(
            tool for tool in dynamic_web_tools if tool.name in allowed_web_tool_names
        )

        # Resolve discoverable DB-backed skills for this agent and inject:
        # 1. Compact metadata into agent.skills for context assembly
        # 2. Executable skill tool definitions into agent_tools
        # 3. Initially attached skill tool ids for deferred exposure defaults
        try:
            async with session_factory() as db_session:
                resolved_skills = await resolve_skills_for_agent(
                    db_session, tool_agent, owner_email=user_email
                )
            if not _management_tools_allowed(tool_agent, access_context):
                resolved_skills.skills = [
                    skill
                    for skill in resolved_skills.skills
                    if skill.skill_id != "cognis-agent-manager"
                ]
            if resolved_skills.skills:
                runtime_metadata["skill_manifests"] = [
                    {
                        "skill_id": skill.skill_id,
                        "version_id": skill.version_id,
                        "content_hash": skill.content_hash,
                        "asset_manifest": [
                            asset.model_dump(mode="json", exclude_none=True)
                            for asset in skill.asset_manifest
                        ],
                    }
                    for skill in resolved_skills.skills
                    if skill.asset_manifest or skill.tools
                ]
                # Build compact metadata for the immutable prompt prefix
                metadata = build_available_skills_metadata(resolved_skills)
                if metadata:
                    if not isinstance(tool_agent.skills, dict):
                        tool_agent.skills = {}
                    attached_tool_ids_by_skill = attached_skill_tool_ids_by_skill(resolved_skills)
                    auto_loaded_skill_contexts: list[str] = []
                    auto_loaded_skill_tool_ids: set[str] = set()
                    auto_loaded_skill_ids: list[str] = []
                    for skill in resolved_skills.skills:
                        if not skill.auto_load_instructions:
                            continue
                        _, load_metadata = materialize_loaded_skill_context(
                            skill_id=skill.skill_id,
                            name=skill.name,
                            description=skill.description,
                            instructions=skill.instructions,
                            tools=[tool.model_dump(mode="json") for tool in skill.tools],
                            templates=skill.prompt_templates,
                            asset_refs=skill.asset_manifest,
                            steps=skill.steps,
                            tags=skill.tags,
                            linked_tool_ids=skill.linked_tool_ids,
                            attach_to_all_agents=skill.auto_load,
                        )
                        protected_context = load_metadata.get("protected_context")
                        if isinstance(protected_context, str) and protected_context.strip():
                            auto_loaded_skill_contexts.append(protected_context)
                            auto_loaded_skill_ids.append(skill.skill_id)
                        discovered_ids = load_metadata.get("discovered_tool_ids")
                        if isinstance(discovered_ids, list):
                            auto_loaded_skill_tool_ids.update(
                                str(tool_id)
                                for tool_id in discovered_ids
                                if isinstance(tool_id, str) and tool_id.strip()
                            )
                    tool_agent.skills["_available_skills_metadata"] = metadata
                    tool_agent.skills["_attached_skill_tool_ids"] = sorted(
                        attached_skill_tool_ids(resolved_skills)
                    )
                    tool_agent.skills["_attached_skill_tool_ids_by_skill"] = (
                        attached_tool_ids_by_skill
                    )
                    if auto_loaded_skill_contexts:
                        tool_agent.skills["_auto_loaded_skill_contexts"] = (
                            auto_loaded_skill_contexts
                        )
                        tool_agent.skills["_auto_loaded_skill_ids"] = auto_loaded_skill_ids
                    if auto_loaded_skill_tool_ids:
                        tool_agent.skills["_auto_loaded_skill_tool_ids"] = sorted(
                            auto_loaded_skill_tool_ids
                        )
                    tool_agent.skills["_runtime_skill_summaries"] = [
                        {
                            "skill_id": skill.skill_id,
                            "name": skill.name,
                            "description": skill.description,
                            "attached": skill.attached,
                            "auto_load": skill.auto_load,
                            "auto_load_instructions": skill.auto_load_instructions,
                            "tags": list(getattr(skill, "tags", []) or []),
                            "linked_tool_ids": list(getattr(skill, "linked_tool_ids", []) or []),
                        }
                        for skill in resolved_skills.skills
                    ]

                # Add executable skill tools to the agent tool set
                skill_tool_defs = discoverable_skill_tools_to_definitions(resolved_skills)
                agent_tools.extend(skill_tool_defs)
        except Exception:
            logger.warning(
                "Failed to resolve DB-backed skills for agent",
                extra={"extra_data": {"agent_id": tool_agent.agent_id}},
                exc_info=True,
            )

        # Only include executor-native tools that are enabled on this executor.
        # Controller-side tools (builtin) are always available regardless.
        disabled_categories = (
            set(tool_agent.tools.get("disabled_categories") or [])
            if isinstance(tool_agent.tools, dict)
            else set()
        )
        disabled_tools = (
            set(tool_agent.tools.get("disabled_tools") or [])
            if isinstance(tool_agent.tools, dict)
            else set()
        )
        disabled_mcp_servers = (
            disabled_mcp_server_keys(tool_agent.tools)
            if isinstance(tool_agent.tools, dict)
            else set()
        )
        filtered: list[ToolDefinition] = []
        for tool in agent_tools:
            if tool_disabled_by_agent_config(
                tool,
                disabled_categories=disabled_categories,
                disabled_tools=disabled_tools,
                disabled_mcp_servers=disabled_mcp_servers,
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

        resolved_type = executor_config.get("executor_type", "in_process")
        if not is_executor_type_allowed(resolved_type, policy):
            _raise_runtime_resolution_error(
                f"Executor type '{resolved_type}' is disabled by deployment policy",
                executor_config=executor_config,
                selection_source=selection_source,
                hard_bound=hard_bound_executor,
            )

        # Remote executors advertise executor-assigned MCP tools through tool.list.
        # In-process execution is only allowed for the explicitly selected
        # executor row; legacy agent-local MCP config is intentionally ignored
        # here to avoid controller fallback.
        mcp_servers: list[MCPServerConfig] = []
        if resolved_type == "in_process":
            mcp_diagnostics: dict[str, Any] = {}
            mcp_servers = await _resolve_executor_mcp_servers(
                executor_config,
                session_factory,
                providers=providers,
                user_email=user_email,
                conversation_id=conversation_id or getattr(access_context, "conversation_id", None),
                task_id=task_id,
                session_id=getattr(access_context, "session_id", None),
                step_name=getattr(access_context, "step_name", None),
                step_run_id=getattr(access_context, "step_run_id", None),
                delivery_mode="default" if task_id else "silent",
                diagnostics=mcp_diagnostics,
            )
            if mcp_diagnostics:
                runtime_metadata["mcp_servers"] = list(mcp_diagnostics.get("mcp_servers") or [])
                runtime_metadata["warnings"] = list(mcp_diagnostics.get("warnings") or [])
            if disabled_mcp_servers:
                mcp_servers = [
                    server
                    for server in mcp_servers
                    if f"local_mcp:{server.server_id or server.name}" not in disabled_mcp_servers
                ]
            secret_owner_email = executor_config.get("executor_owner_email", user_email)
            secrets = await providers.secrets.resolve_for_execution(tool_agent, secret_owner_email)
            handler_map = _build_handler_map(
                session_factory,
                getattr(providers.executor, "status_provider", None),
                getattr(providers, "guardrails", None),
                getattr(providers, "compaction_strategy", None),
                knowledgebase_service,
            )
            handle = await providers.executor.spawn(
                ExecutorConfig(
                    executor_id=f"{executor_config['executor_id']}:run:{uuid4().hex}",
                    tools=agent_tools,
                    tool_handlers=handler_map,
                    mcp_servers=mcp_servers,
                    secrets=secrets,
                    metadata=runtime_metadata,
                )
            )
            connection = await providers.executor.get_executor(handle)
            registry = getattr(connection, "registry", None)
            if registry is None:
                raise RuntimeError("Selected in-process executor did not expose a registry")

            async def cleanup() -> None:
                await providers.executor.cancel(handle)

            env_snapshot = build_local_executor_environment(
                executor_id=handle.executor_id,
                executor_type=handle.executor_type,
                source="direct_in_process_executor",
            )
            return ResolvedStepRuntime(
                tool_registry=registry,
                executor_connection=connection,
                cleanup=cleanup,
                executor_environment=env_snapshot,
                runtime_info=_runtime_info(
                    executor_config=executor_config,
                    selection_source=selection_source,
                    hard_bound=hard_bound_executor,
                    runtime_source="direct_in_process_executor",
                    fallback_used=False,
                    executor_pin_fallback_notice=executor_config.get(
                        "executor_pin_fallback_notice"
                    ),
                    environment=env_snapshot,
                    inventory_tool_count=len(registry.list_tools()),
                    tool_agent=tool_agent,
                    executor_agent=executor_agent,
                ),
                executor_pool=executor_pool_obj,
                active_executor_id=executor_config.get("executor_id"),
            )

        if resolved_type in ("websocket", "subprocess"):
            # Remote executor — merge executor-advertised tools with
            # controller-side builtin tools (memory, orchestration, etc.)
            # and Intaris MCP tools.  Executor-native and web tools come
            # from the executor; everything else is handled locally by
            # the controller's ToolRouter.
            from cognis.providers.executor.websocket import WebSocketExecutorProvider

            ws_provider: WebSocketExecutorProvider = providers.executor.websocket
            executor_id = executor_config.get("executor_id", "")
            conn = ws_provider.get_connection(executor_id)
            runtime_ready = bool(
                executor_config.get("runtime_state", "offline") in {"active", "degraded"}
                and int(executor_config.get("desired_config_version", 0) or 0)
                == int(executor_config.get("applied_config_version", 0) or 0)
            )
            if conn is not None and runtime_ready:
                try:
                    disabled_categories = (
                        set(tool_agent.tools.get("disabled_categories") or [])
                        if isinstance(tool_agent.tools, dict)
                        else set()
                    )
                    disabled_tools = (
                        set(tool_agent.tools.get("disabled_tools") or [])
                        if isinstance(tool_agent.tools, dict)
                        else set()
                    )
                    disabled_mcp_servers = (
                        disabled_mcp_server_keys(tool_agent.tools)
                        if isinstance(tool_agent.tools, dict)
                        else set()
                    )

                    remote_tools = await conn.list_tools()
                    merge_result = await _merge_remote_runtime_inventory(
                        remote_tools_data=remote_tools,
                        agent_tools=agent_tools,
                        providers=providers,
                        agent=tool_agent,
                        disabled_categories=disabled_categories,
                        disabled_tools=disabled_tools,
                        disabled_mcp_servers=disabled_mcp_servers,
                    )
                    handler_map = _build_handler_map(
                        session_factory,
                        getattr(providers.executor, "status_provider", None),
                        getattr(providers, "guardrails", None),
                        getattr(providers, "compaction_strategy", None),
                        knowledgebase_service,
                    )
                    remote_registry = _build_remote_runtime_registry(
                        merge_result.tools,
                        handler_map,
                    )
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
                        runtime_info=_runtime_info(
                            executor_config=executor_config,
                            selection_source=selection_source,
                            hard_bound=hard_bound_executor,
                            runtime_source="remote_executor",
                            fallback_used=False,
                            executor_pin_fallback_notice=executor_config.get(
                                "executor_pin_fallback_notice"
                            ),
                            environment=env_snapshot,
                            inventory_tool_count=len(all_tools),
                            tool_agent=tool_agent,
                            executor_agent=executor_agent,
                        ),
                        executor_pool=executor_pool_obj,
                        active_executor_id=executor_id,
                    )
                except Exception as exc:
                    message = f"Selected executor '{executor_id}' failed while listing tools"
                    from cognis.core.executor_pin_lifecycle import (
                        fallback_active_executor_after_remote_failure,
                    )

                    fallback_lifecycle = await fallback_active_executor_after_remote_failure(
                        session_factory=session_factory,
                        conversation_id=conversation_id,
                        task_id=task_id,
                        pool=executor_pool_obj,
                        active_executor_id=executor_id,
                        reason="secondary executor failed while listing tools",
                    )
                    if fallback_lifecycle.notice is not None:
                        notice = fallback_lifecycle.notice
                        return await factory(
                            agent,
                            user_email,
                            executor_agent=executor_agent,
                            access_context=access_context,
                            conversation_id=conversation_id,
                            task_id=task_id,
                            _executor_pin_fallback_notice={
                                "previous_executor_id": notice.previous_executor_id,
                                "new_executor_id": notice.new_executor_id,
                                "reason": notice.reason,
                                "ui_message": notice.ui_message,
                                "llm_message": notice.llm_message,
                            },
                            _executor_pin_fallback_retried=True,
                        )
                    logger.warning(
                        "Failed to get tools from selected remote executor",
                        extra={
                            "extra_data": {
                                "executor_id": executor_id,
                                "selection_source": selection_source,
                                "hard_bound": hard_bound_executor,
                            }
                        },
                        exc_info=True,
                    )
                    raise RuntimeError(message) from exc
            from cognis.core.executor_pin_lifecycle import (
                fallback_active_executor_after_remote_failure,
            )

            fallback_lifecycle = await fallback_active_executor_after_remote_failure(
                session_factory=session_factory,
                conversation_id=conversation_id,
                task_id=task_id,
                pool=executor_pool_obj,
                active_executor_id=executor_id,
                reason="secondary executor is not connected or not ready",
            )
            if fallback_lifecycle.notice is not None:
                notice = fallback_lifecycle.notice
                return await factory(
                    agent,
                    user_email,
                    executor_agent=executor_agent,
                    access_context=access_context,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    _executor_pin_fallback_notice={
                        "previous_executor_id": notice.previous_executor_id,
                        "new_executor_id": notice.new_executor_id,
                        "reason": notice.reason,
                        "ui_message": notice.ui_message,
                        "llm_message": notice.llm_message,
                    },
                    _executor_pin_fallback_retried=True,
                )
            message = f"Selected executor '{executor_id}' is not connected or not ready"
            _raise_runtime_resolution_error(
                message,
                executor_config=executor_config,
                selection_source=selection_source,
                hard_bound=hard_bound_executor,
            )

        raise RuntimeError(f"Executor type '{resolved_type}' is not supported for agent execution")

    return factory


async def _resolve_web_config(
    providers: Any,
    user_email: str,
) -> dict[str, Any]:
    """Resolve web backend configuration from settings and secrets.

    Search and fetch are now selected independently. The legacy single
    ``web.backend`` setting is honoured as a fallback for executors that
    have not yet been migrated.
    """
    legacy_backend = "direct"
    search_backend = "direct"
    fetch_backend = "direct"
    fetch_fallback_browser = True
    searxng_url = ""
    searxng_engines = ""
    searxng_categories = ""
    searxng_language = ""
    browser_fetch_session_idle = 60
    browser_fetch_wait_timeout = 30
    browser_fetch_navigation_timeout = 60
    browser_fetch_wait_until = "domcontentloaded"
    browser_fetch_network_idle = 3
    browser_fetch_headed_fallback = False
    concurrency: dict[str, Any] = {
        "global_cap": 32,
        "per_host_cap": 4,
        "backend_caps": {
            "direct": 16,
            "tavily": 8,
            "brave": 2,
            "searxng": 4,
            "browser": 4,
        },
        "rate_limits_qps": {
            "direct": 0.0,
            "tavily": 5.0,
            "brave": 1.0,
            "searxng": 5.0,
            "browser": 0.0,
        },
    }
    web_secrets: dict[str, str] = {}

    session_factory = getattr(providers, "_session_factory", None)
    if session_factory is not None:
        try:
            from cognis.store.queries import get_setting_value

            async with session_factory() as session:
                legacy_value = await get_setting_value(session, "web.backend", "direct")
                if isinstance(legacy_value, str) and legacy_value:
                    legacy_backend = legacy_value
                # Split keys win when set; fall back to legacy single axis.
                search_value = await get_setting_value(
                    session, "web.search_backend", legacy_backend
                )
                if isinstance(search_value, str) and search_value:
                    search_backend = search_value
                fetch_value = await get_setting_value(session, "web.fetch_backend", legacy_backend)
                if isinstance(fetch_value, str) and fetch_value:
                    fetch_backend = fetch_value
                fallback_value = await get_setting_value(
                    session, "web.fetch_fallback_browser", True
                )
                if isinstance(fallback_value, bool):
                    fetch_fallback_browser = fallback_value
                for key, target in (
                    ("web.searxng_url", "searxng_url"),
                    ("web.searxng_engines", "searxng_engines"),
                    ("web.searxng_categories", "searxng_categories"),
                    ("web.searxng_language", "searxng_language"),
                ):
                    raw = await get_setting_value(session, key, "")
                    if isinstance(raw, str):
                        if target == "searxng_url":
                            searxng_url = raw
                        elif target == "searxng_engines":
                            searxng_engines = raw
                        elif target == "searxng_categories":
                            searxng_categories = raw
                        else:
                            searxng_language = raw
                idle_value = await get_setting_value(
                    session, "web.browser_fetch.session_idle_seconds", 60
                )
                if isinstance(idle_value, int) and idle_value > 0:
                    browser_fetch_session_idle = idle_value
                wait_value = await get_setting_value(
                    session, "web.browser_fetch.wait_timeout_seconds", 30
                )
                if isinstance(wait_value, int) and wait_value > 0:
                    browser_fetch_wait_timeout = wait_value
                navigation_value = await get_setting_value(
                    session, "web.browser_fetch.navigation_timeout_seconds", 60
                )
                if isinstance(navigation_value, int) and navigation_value > 0:
                    browser_fetch_navigation_timeout = navigation_value
                wait_until_value = await get_setting_value(
                    session, "web.browser_fetch.wait_until", "domcontentloaded"
                )
                if isinstance(wait_until_value, str) and wait_until_value:
                    browser_fetch_wait_until = wait_until_value
                network_idle_value = await get_setting_value(
                    session, "web.browser_fetch.network_idle_after_dom_seconds", 3
                )
                if isinstance(network_idle_value, int) and network_idle_value >= 0:
                    browser_fetch_network_idle = network_idle_value
                headed_value = await get_setting_value(
                    session, "web.browser_fetch.headed_fallback_enabled", False
                )
                if isinstance(headed_value, bool):
                    browser_fetch_headed_fallback = headed_value

                async def _read_int(key: str, default: int) -> int:
                    raw = await get_setting_value(session, key, default)
                    return int(raw) if isinstance(raw, int) and raw > 0 else default

                async def _read_float(key: str, default: float) -> float:
                    raw = await get_setting_value(session, key, default)
                    if isinstance(raw, (int, float)) and raw >= 0:
                        return float(raw)
                    return default

                concurrency["global_cap"] = await _read_int("web.concurrency.global_cap", 32)
                concurrency["per_host_cap"] = await _read_int("web.concurrency.per_host_cap", 4)
                for backend_name, default_cap in (
                    ("direct", 16),
                    ("tavily", 8),
                    ("brave", 2),
                    ("searxng", 4),
                    ("browser", 4),
                ):
                    concurrency["backend_caps"][backend_name] = await _read_int(
                        f"web.concurrency.{backend_name}_cap", default_cap
                    )
                for backend_name, default_qps in (
                    ("tavily", 5.0),
                    ("brave", 1.0),
                    ("searxng", 5.0),
                ):
                    concurrency["rate_limits_qps"][backend_name] = await _read_float(
                        f"web.rate_limit.{backend_name}_qps", default_qps
                    )
        except Exception:
            logger.debug("web: failed to read web settings", exc_info=True)

    # Read API keys from secrets provider
    if hasattr(providers, "secrets") and hasattr(providers.secrets, "get_secret"):
        for secret_name in ("tavily_api_key", "brave_api_key"):
            try:
                value = await providers.secrets.get_secret(secret_name, SYSTEM_USER_EMAIL)
                if value:
                    web_secrets[secret_name] = value
            except KeyError:
                pass  # Secret not configured — expected
            except Exception:
                logger.debug("web: failed to read secret %s", secret_name)

    available_search = ["direct"]
    if web_secrets.get("tavily_api_key"):
        available_search.append("tavily")
    if web_secrets.get("brave_api_key"):
        available_search.append("brave")
    if searxng_url.strip():
        available_search.append("searxng")

    available_fetch = ["direct"]
    if web_secrets.get("tavily_api_key"):
        available_fetch.append("tavily")
    # ``browser`` becomes available iff the executor exposes a BrowserManager,
    # which we resolve at executor configure time. Add a hint here so the
    # remote executor knows it's allowed once it has a manager.
    available_fetch.append("browser")

    available_union = sorted({*available_search, *available_fetch})

    return {
        "web_backend": legacy_backend,
        "web_search_backend": search_backend,
        "web_fetch_backend": fetch_backend,
        "web_fetch_fallback_browser": fetch_fallback_browser,
        "web_searxng_url": searxng_url,
        "web_searxng_engines": searxng_engines,
        "web_searxng_categories": searxng_categories,
        "web_searxng_language": searxng_language,
        "web_browser_fetch_session_idle_seconds": browser_fetch_session_idle,
        "web_browser_fetch_wait_timeout_seconds": browser_fetch_wait_timeout,
        "web_browser_fetch_navigation_timeout_seconds": browser_fetch_navigation_timeout,
        "web_browser_fetch_wait_until": browser_fetch_wait_until,
        "web_browser_fetch_network_idle_after_dom_seconds": browser_fetch_network_idle,
        "web_browser_fetch_headed_fallback_enabled": browser_fetch_headed_fallback,
        "web_concurrency": concurrency,
        "web_secrets": web_secrets,
        "web_available_backends": available_union,
        "web_available_search_backends": available_search,
        "web_available_fetch_backends": available_fetch,
    }


async def _resolve_executor_mcp_servers(
    executor_config: dict[str, Any] | None,
    session_factory: Any,
    *,
    providers: Any | None = None,
    user_email: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    step_name: str | None = None,
    step_run_id: str | None = None,
    session_id: str | None = None,
    delivery_mode: str | None = "silent",
    diagnostics: dict[str, Any] | None = None,
) -> list[MCPServerConfig]:
    """Resolve MCP servers assigned to an executor via config.mcp_server_ids."""
    from cognis.store.queries import get_mcp_server, get_setting_value

    if not executor_config:
        return []
    config = executor_config.get("config") or {}
    server_ids = config.get(MCP_SERVER_IDS_KEY, [])
    if not isinstance(server_ids, list) or not server_ids:
        return []

    servers: list[MCPServerConfig] = []
    skipped_statuses: list[dict[str, Any]] = []
    warnings: list[str] = []
    async with session_factory() as session:
        tool_timeout_raw = await get_setting_value(session, "mcp.tool_timeout_seconds", 300)
        connect_timeout_raw = await get_setting_value(session, "mcp.connect_timeout_seconds", 15)
        tool_timeout = _coerce_positive_int(tool_timeout_raw, 300)
        connect_timeout = _coerce_positive_int(connect_timeout_raw, 15)
        for sid in server_ids:
            row = await get_mcp_server(
                session,
                str(sid),
                owner_email=executor_config.get("executor_owner_email")
                if executor_config
                else None,
                include_shared=True,
            )
            if row is None:
                logger.warning(
                    "MCP server not found",
                    extra={"extra_data": {"server_id": sid}},
                )
                continue
            if row.status != "active":
                continue
            invalid_reason = invalid_mcp_config_reason(
                transport=row.transport,
                command=row.command,
                url=row.url,
                env=row.env,
                headers=row.headers,
                auth_config=row.auth_config,
            )
            if invalid_reason is not None:
                logger.warning(
                    "Skipping invalid MCP server config",
                    extra={"extra_data": {"server_id": sid, "reason": invalid_reason}},
                )
                continue
            headers = row.headers or {}
            auth_config = effective_mcp_auth_config(row.auth_config, headers)
            oauth_service = None
            if providers is not None:
                oauth_service = getattr(providers, "mcp_oauth_service", None) or getattr(
                    providers, "_mcp_oauth_service", None
                )
            if auth_config.type == "oauth2" and (oauth_service is None or not user_email):
                warning = (
                    f"MCP server {row.name} requires OAuth authorization, "
                    "but no OAuth context is available."
                )
                warnings.append(warning)
                skipped_statuses.append(
                    oauth_required_mcp_status(
                        server_id=sid,
                        server_name=row.name,
                        reason="oauth_context_unavailable",
                    )
                )
                logger.warning(
                    "Skipping OAuth MCP server without OAuth context",
                    extra={
                        "extra_data": {
                            "server_id": sid,
                            "server_name": row.name,
                            "has_oauth_service": oauth_service is not None,
                            "has_user_email": bool(user_email),
                        }
                    },
                )
                continue
            if auth_config.type == "oauth2" and oauth_service is not None and user_email:
                try:
                    result = await oauth_service.inject_authorization_header(
                        user_email=user_email,
                        server=row,
                        headers={k: v for k, v in headers.items() if k.lower() != "authorization"},
                        conversation_id=conversation_id,
                        task_id=task_id,
                        step_name=step_name,
                        step_run_id=step_run_id,
                        session_id=session_id,
                        delivery_mode=delivery_mode,
                    )
                except MCPOAuthError as exc:
                    warning = (
                        f"MCP server {row.name} requires OAuth authorization, "
                        "but authorization metadata could not be resolved."
                    )
                    warnings.append(warning)
                    skipped_statuses.append(
                        oauth_required_mcp_status(
                            server_id=sid,
                            server_name=row.name,
                            reason=str(exc)[:240],
                        )
                    )
                    logger.warning(
                        "Skipping OAuth MCP server with unresolved authorization metadata",
                        extra={
                            "extra_data": {
                                "server_id": sid,
                                "server_name": row.name,
                                "reason": str(exc)[:240],
                            }
                        },
                    )
                    continue
                if result.authorization_required:
                    warning = (
                        f"MCP server {row.name} requires OAuth authorization before "
                        "tools can be discovered."
                    )
                    warnings.append(warning)
                    skipped_statuses.append(
                        oauth_required_mcp_status(
                            server_id=sid,
                            server_name=row.name,
                            reason=result.reason,
                            transaction_id=result.transaction_id,
                            authorization_url=result.authorization_url,
                        )
                    )
                    logger.warning(
                        "OAuth MCP server requires authorization",
                        extra={
                            "extra_data": {
                                "server_id": sid,
                                "reason": result.reason,
                                "transaction_id": result.transaction_id,
                            }
                        },
                    )
                    continue
                headers = result.headers
                auth_config = {"type": "static_headers"}
            servers.append(
                MCPServerConfig(
                    server_id=row.server_id,
                    name=row.name,
                    transport=row.transport,
                    command=row.command,
                    url=row.url,
                    args=row.args or [],
                    env=row.env or {},
                    headers=headers,
                    auth_config=auth_config,
                    timeout_seconds=max(int(row.timeout_seconds or 0), tool_timeout),
                    connect_timeout_seconds=connect_timeout,
                )
            )
    if servers:
        logger.debug(
            "Resolved executor MCP servers",
            extra={"extra_data": {"count": len(servers)}},
        )
    if diagnostics is not None:
        if skipped_statuses:
            diagnostics.setdefault("mcp_servers", []).extend(skipped_statuses)
        if warnings:
            diagnostics.setdefault("warnings", []).extend(warnings)
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
    disabled_mcp_servers: set[str] | None = None,
    intaris_result: IntarisMCPResolutionResult | None = None,
) -> RemoteInventoryMergeResult:
    """Build merged runtime-visible inventory for remote executors."""
    warnings: list[str] = []
    merged: list[ToolDefinition] = []
    merged_names: set[str] = set()
    collision_count = 0
    disabled_mcp_servers = disabled_mcp_servers or set()

    builtin_defs = sorted(
        [tool for tool in agent_tools if tool.source.type in ("builtin", "skill")],
        key=_tool_collision_identity,
    )
    reserved_controller_names = set(DEFAULT_OFF_BUILTIN_TOOLS)

    remote_defs: list[ToolDefinition] = []
    for tool_data in remote_tools_data:
        tool_def = ToolDefinition.model_validate(tool_data)
        if tool_disabled_by_agent_config(
            tool_def,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools,
            disabled_mcp_servers=disabled_mcp_servers,
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
        if tool.name in reserved_controller_names:
            collision_count += 1
            _append_warning(
                warnings,
                "Remote executor tool was hidden because its name is reserved by a controller tool.",
            )
            continue
        if tool.name in merged_names:
            collision_count += 1
            _append_warning(
                warnings,
                "Remote executor reported duplicate tool names; later duplicates were ignored.",
            )
            continue
        merged.append(tool)
        merged_names.add(tool.name)

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
            disabled_mcp_servers,
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
    disabled_mcp_servers: set[str] | None = None,
) -> IntarisMCPResolutionResult:
    result = IntarisMCPResolutionResult()
    disabled_mcp_servers = disabled_mcp_servers or set()
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
            if tool_disabled_by_agent_config(
                tool,
                disabled_categories=disabled_categories,
                disabled_tools=disabled_tools,
                disabled_mcp_servers=disabled_mcp_servers,
            ):
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
        disambiguate_mcp_tool_name_collisions(tools),
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
    disabled_mcp_servers: set[str] | None = None,
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

    disabled_server_keys = (
        disabled_mcp_servers
        if disabled_mcp_servers is not None
        else disabled_mcp_server_keys(agent.tools)
        if isinstance(agent.tools, dict)
        else set()
    )
    allowed_names = set(server_names)
    disabled_names = {
        key.removeprefix("intaris_mcp:")
        for key in disabled_server_keys
        if key.startswith("intaris_mcp:")
    }
    allowed_names -= disabled_names
    if not allowed_names:
        return IntarisMCPResolutionResult()
    disabled_mcp_servers = disabled_server_keys
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
            disabled_mcp_servers=disabled_mcp_servers,
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
            disabled_mcp_servers=disabled_mcp_servers,
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
        if tool_disabled_by_agent_config(
            tool,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools,
            disabled_mcp_servers=disabled_mcp_servers,
        ):
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
            disabled_mcp_servers=disabled_mcp_servers,
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
