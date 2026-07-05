"""Tool discovery routes."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError
from sqlalchemy import select

from cognis.api.common import (
    api_exception,
    check_agent_access,
    require_current_user,
)
from cognis.api.mcp_reconfigure import schedule_mcp_server_executor_reconfigure_for_app
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
    ToolClassificationActionResponse,
    ToolClassificationOverrideRequest,
    ToolClassificationRequeueRequest,
    ToolResponse,
)
from cognis.api.runtime_support import (
    _merge_remote_runtime_inventory,
    _resolve_executor_mcp_servers,
    _resolve_intaris_mcp_tools,
    disabled_mcp_server_keys,
    select_static_tools,
    tool_disabled_by_agent_config,
)
from cognis.api.serializers import agent_to_response, mcp_server_to_response, tool_to_response
from cognis.api.tool_inventory import (
    build_intaris_tool_definition,
    collect_unique_observed_local_mcp_tools,
    extract_intaris_aggregated_raw_tool_name,
    extract_intaris_aggregated_server_name,
)
from cognis.core.executor_policy import is_executor_row_usable, load_executor_policy
from cognis.core.executor_resolution import is_tool_enabled, select_executor_for_agent
from cognis.models.agent import AgentDefinition, AgentPermissions
from cognis.models.tool import (
    AUTO_PROFILE_GROUPS,
    MCP_SERVER_IDS_KEY,
    ExecutorConfig,
    MCPServerConfig,
    ToolCapability,
    ToolDefinition,
    ToolSource,
    effective_mcp_auth_config,
    stable_tool_id,
    tool_display_name,
)
from cognis.ownership import is_shared_owner_email
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.models import ToolClassificationRow
from cognis.store.queries import (
    create_mcp_server,
    delete_mcp_server,
    delete_tool_classification_override,
    get_agent,
    get_mcp_server,
    get_tool_classification_override_rows,
    list_agents,
    list_executors,
    mcp_server_referenced_by_executors,
    update_mcp_server,
    upsert_tool_classification_override,
)
from cognis.store.queries import (
    list_mcp_servers as list_global_mcp_servers,
)
from cognis.tools.builtin.workflow import (
    STEP_REQUEST_QUESTIONS_TOOL,
    STEP_TODO_LIST_TOOL,
    STEP_TODO_WRITE_TOOL,
)
from cognis.tools.classification import resolve_tool_classifications
from cognis.tools.executor.definitions import executor_tool_definitions
from cognis.tools.mcp import (
    MCPClient,
    build_mcp_client,
    canonicalize_mcp_headers,
    invalid_mcp_config_reason,
    mcp_tools_to_definitions,
)

router = APIRouter(tags=["tools"])


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _sanitize_mcp_error(error: Exception) -> str:
    return f"{error.__class__.__name__}: {str(error)[:300]}"


def _executor_observed_tools_are_current(row: Any) -> bool:
    return int(getattr(row, "desired_config_version", 0) or 0) == int(
        getattr(row, "applied_config_version", 0) or 0
    )


def _controller_catalog_tools() -> list[ToolDefinition]:
    """Return core controller tools for catalog display.

    These are not normal assignable executor tools. They are exposed by the
    agent loop according to context policy, but the catalog should still show
    their canonical names and chat/workflow aliases.
    """

    return [
        ToolDefinition(
            name=STEP_REQUEST_QUESTIONS_TOOL.name,
            description=STEP_REQUEST_QUESTIONS_TOOL.description,
            parameters=STEP_REQUEST_QUESTIONS_TOOL.parameters,
            source=ToolSource(type="controller"),
            category="workflow",
            profile_group="system",
            read_only=False,
            configurable=False,
            canonical_name=STEP_REQUEST_QUESTIONS_TOOL.name,
            primary_name=STEP_REQUEST_QUESTIONS_TOOL.name,
            aliases=[{"name": "request_user_input", "surface": "direct_chat"}],
            surfaces={
                "workflow_step": STEP_REQUEST_QUESTIONS_TOOL.name,
                "direct_chat": "request_user_input",
            },
        ),
        ToolDefinition(
            name=STEP_TODO_WRITE_TOOL.name,
            description=STEP_TODO_WRITE_TOOL.description,
            parameters=STEP_TODO_WRITE_TOOL.parameters,
            source=ToolSource(type="controller"),
            category="workflow",
            profile_group="system",
            read_only=False,
            configurable=False,
            canonical_name=STEP_TODO_WRITE_TOOL.name,
            primary_name=STEP_TODO_WRITE_TOOL.name,
            aliases=[{"name": "todo_write", "surface": "direct_chat"}],
            surfaces={
                "workflow_step": STEP_TODO_WRITE_TOOL.name,
                "direct_chat": "todo_write",
            },
        ),
        ToolDefinition(
            name=STEP_TODO_LIST_TOOL.name,
            description=STEP_TODO_LIST_TOOL.description,
            parameters=STEP_TODO_LIST_TOOL.parameters,
            source=ToolSource(type="controller"),
            category="workflow",
            profile_group="system",
            read_only=True,
            configurable=False,
            canonical_name=STEP_TODO_LIST_TOOL.name,
            primary_name=STEP_TODO_LIST_TOOL.name,
            aliases=[{"name": "todo_list", "surface": "direct_chat"}],
            surfaces={
                "workflow_step": STEP_TODO_LIST_TOOL.name,
                "direct_chat": "todo_list",
            },
        ),
    ]


def _mcp_is_shared(row: Any) -> bool:
    return is_shared_owner_email(getattr(row, "owner_email", None))


async def _discover_local_mcp_tools(
    request: Request,
    agent: AgentDefinition,
    user_email: str,
    *,
    timeout_seconds: int = 30,
) -> list[ToolDefinition]:
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
    definitions = [
        tool if isinstance(tool, ToolDefinition) else ToolDefinition.model_validate(tool)
        for tool in tools
    ]
    return [tool for tool in definitions if tool.source.type == "local_mcp"]


@router.get("/api/v1/tools", response_model=list[ToolResponse])
async def list_tools(request: Request) -> list[ToolResponse]:
    require_current_user(request)
    kb_enabled = bool(getattr(request.app.state, "knowledgebase_enabled", False))
    controller_tool_names = {
        STEP_REQUEST_QUESTIONS_TOOL.name,
        STEP_TODO_WRITE_TOOL.name,
        STEP_TODO_LIST_TOOL.name,
    }
    static_tools = [
        tool
        for tool in select_static_tools(knowledgebase_enabled=kb_enabled)
        if tool.name not in controller_tool_names
    ]
    tools = await resolve_tool_classifications(
        [*_controller_catalog_tools(), *static_tools],
        session_factory=request.app.state.session_factory,
        owner_email=None,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )
    return [tool_to_response(tool) for tool in tools]


@router.get("/api/v1/tools/local-mcp/observed", response_model=list[ToolResponse])
async def list_observed_local_mcp_tools(request: Request) -> list[ToolResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        executors = await list_executors(session, owner_email=user.email, include_shared=True)

    unique: dict[str, ToolDefinition] = {}
    for row in executors:
        if not _executor_observed_tools_are_current(row):
            continue
        observed = collect_unique_observed_local_mcp_tools(getattr(row, "observed_tools", None))
        for tool in observed:
            unique.setdefault(stable_tool_id(tool), tool)

    ordered = sorted(
        unique.values(),
        key=lambda tool: (
            tool.source.server_name or "",
            tool.source.raw_tool_name or tool.name,
            tool.name,
        ),
    )
    ordered = await resolve_tool_classifications(
        ordered,
        session_factory=request.app.state.session_factory,
        owner_email=user.email,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )
    return [tool_to_response(tool) for tool in ordered]


@router.get("/api/v1/tools/executor", response_model=list[ToolResponse])
async def list_executor_tools(request: Request) -> list[ToolResponse]:
    """List executor-native tools with their definitions."""
    require_current_user(request)
    tools = await resolve_tool_classifications(
        executor_tool_definitions(),
        session_factory=request.app.state.session_factory,
        owner_email=None,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )
    return [tool_to_response(tool) for tool in tools]


@router.get("/api/v1/agents/{agent_id}/effective-tools", response_model=EffectiveToolsResponse)
async def get_agent_effective_tools(request: Request, agent_id: str) -> EffectiveToolsResponse:
    async with request.app.state.session_factory() as session:
        row = await get_agent(session, agent_id)
    if row is None:
        raise api_exception(404, "not_found", "Agent not found")
    access = await check_agent_access(request, row, required="view")
    agent = AgentDefinition.model_validate(agent_to_response(row).model_dump())
    executor_owner_email = (
        access.user.email if access.executor_scope == "grantee_executor" else agent.owner_email
    )
    require_default_executor = False
    if access.executor_scope == "grantee_executor" and access.grant is not None:
        overrides = getattr(access.grant, "grantee_overrides", None)
        execution = overrides.get("execution") if isinstance(overrides, dict) else None
        agent.execution = execution if isinstance(execution, dict) else {}
        require_default_executor = not agent.execution
    return await _resolve_effective_tools_response(
        request,
        agent,
        acting_user_email=access.user.email,
        executor_owner_email=executor_owner_email,
        require_default_executor=require_default_executor,
        access_context=RuntimeAccessContext(
            user_email=access.user.email,
            agent_id=agent.agent_id,
            agent_owner_email=agent.owner_email,
            agent_type=agent.agent_type,
        ),
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
        agent_type=payload.agent_type,
    )
    return await _resolve_effective_tools_response(
        request,
        agent,
        acting_user_email=user.email,
        executor_owner_email=user.email,
        access_context=RuntimeAccessContext(
            user_email=user.email,
            agent_id=agent.agent_id,
            agent_owner_email=user.email,
            agent_type=agent.agent_type,
        ),
    )


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
    return stable_tool_id(tool)


def _tool_permission(agent: AgentDefinition, tool: ToolDefinition) -> str:
    if agent.permissions is None:
        return "evaluate"
    if isinstance(agent.permissions, dict):
        agent.permissions = AgentPermissions.model_validate(agent.permissions)
    tool_id = _tool_identifier(tool)
    if agent.permissions.tool_permissions and tool_id in agent.permissions.tool_permissions:
        return str(agent.permissions.tool_permissions[tool_id])
    return str(agent.permissions.resolve_permission(tool_display_name(tool), tool_id=tool_id))


async def _get_classification_row_for_tool(
    request: Request,
    *,
    user_email: str,
    tool_id: str,
) -> ToolClassificationRow | None:
    async with request.app.state.session_factory() as session:
        return (
            (
                await session.execute(
                    select(ToolClassificationRow).where(
                        ToolClassificationRow.tool_id == tool_id,
                        ToolClassificationRow.scope_key == user_email,
                    )
                )
            )
            .scalars()
            .first()
        )


async def _get_classification_override_for_tool(
    request: Request,
    *,
    user_email: str,
    tool_id: str,
) -> Any | None:
    async with request.app.state.session_factory() as session:
        rows = await get_tool_classification_override_rows(
            session,
            scope_key=user_email,
            tool_ids=[tool_id],
        )
    return rows[0] if rows else None


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
        for value in [*server.env.values(), *server.headers.values()]
        if isinstance(value, str) and value.startswith("$secret:")
    }
    secrets: dict[str, str] = {}
    for name in secret_names:
        with contextlib.suppress(Exception):
            secrets[name] = await providers.secrets.get_secret(name, user_email)
    clients: list[MCPClient] = []
    discovered: list[ToolDefinition] = []
    try:
        for server in servers:
            client = build_mcp_client(server, secrets)
            await client.connect()
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
                await client.close(suppress_cancelled=True)
    return discovered


async def _resolve_effective_tools_response(
    request: Request,
    agent: AgentDefinition,
    *,
    acting_user_email: str,
    executor_owner_email: str,
    require_default_executor: bool = False,
    access_context: RuntimeAccessContext | None = None,
) -> EffectiveToolsResponse:
    session_factory = request.app.state.session_factory
    policy = await load_executor_policy(session_factory)
    warnings: list[str] = []
    _ = acting_user_email

    async with session_factory() as session:
        executors = await list_executors(
            session,
            owner_email=executor_owner_email,
            include_shared=True,
        )
        execution = agent.execution if isinstance(agent.execution, dict) else None
        if require_default_executor and not execution:
            selected = next(
                (
                    row
                    for row in executors
                    if bool(getattr(row, "is_default", False))
                    and is_executor_row_usable(row, policy, owner_email=executor_owner_email)
                ),
                None,
            )
        else:
            selected = select_executor_for_agent(
                executors,
                execution,
                owner_email=executor_owner_email,
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
    kb_enabled = bool(getattr(request.app.state, "knowledgebase_enabled", False))
    for tool in [
        tool
        for tool in select_static_tools(
            agent, access_context=access_context, knowledgebase_enabled=kb_enabled
        )
        if tool.category != "web"
    ]:
        if tool.source.type == "builtin" or is_tool_enabled(
            tool, selected.enabled_tools or [], selected.enabled_tool_groups or []
        ):
            configured_tools.append(tool)

    # Resolve DB-backed skills and add executable skill tools to preview
    try:
        from cognis.tools.skills import resolve_skills_for_agent, skill_tools_to_definitions

        async with session_factory() as db_session:
            resolved_skills = await resolve_skills_for_agent(
                db_session, agent, owner_email=agent.owner_email
            )
        if resolved_skills.skills:
            skill_tool_defs = skill_tools_to_definitions(resolved_skills)
            configured_tools.extend(skill_tool_defs)
    except Exception:
        warnings.append("Failed to resolve DB-backed skills for preview.")

    disabled_categories = set(
        (agent.tools or {}).get("disabled_categories", []) if isinstance(agent.tools, dict) else []
    )
    disabled_tools_set = set(
        (agent.tools or {}).get("disabled_tools", []) if isinstance(agent.tools, dict) else []
    )
    disabled_mcp_servers = (
        disabled_mcp_server_keys(agent.tools) if isinstance(agent.tools, dict) else set()
    )

    config_ids = (selected.config or {}).get(MCP_SERVER_IDS_KEY, [])
    if isinstance(config_ids, list) and config_ids:
        if selected.executor_type == "in_process":
            mcp_diagnostics: dict[str, Any] = {}
            mcp_servers = await _resolve_executor_mcp_servers(
                {
                    "config": selected.config or {},
                    "executor_owner_email": executor_owner_email,
                },
                session_factory,
                providers=request.app.state.providers,
                user_email=acting_user_email,
                conversation_id=getattr(access_context, "conversation_id", None),
                task_id=getattr(access_context, "task_id", None),
                step_name=getattr(access_context, "step_name", None),
                step_run_id=getattr(access_context, "step_run_id", None),
                session_id=getattr(access_context, "session_id", None),
                delivery_mode="default" if getattr(access_context, "task_id", None) else "silent",
                diagnostics=mcp_diagnostics,
            )
            warnings.extend(mcp_diagnostics.get("warnings") or [])
            if disabled_mcp_servers:
                mcp_servers = [
                    server
                    for server in mcp_servers
                    if f"local_mcp:{server.server_id or server.name}" not in disabled_mcp_servers
                ]
            configured_tools.extend(
                await _discover_temp_mcp_tools(
                    request.app.state.providers,
                    mcp_servers,
                    executor_owner_email,
                )
            )
        elif (
            selected.observed_tools
            and selected.desired_config_version == selected.applied_config_version
        ):
            configured_tools.extend(
                [ToolDefinition.model_validate(item) for item in selected.observed_tools]
            )
        else:
            warnings.append("Executor has assigned MCP servers but no observed manifest yet.")

    intaris_result = await _resolve_intaris_mcp_tools(
        request.app.state.providers,
        agent,
        disabled_categories,
        disabled_tools_set,
        disabled_mcp_servers,
    )
    configured_tools.extend(intaris_result.tools)
    for warning in intaris_result.warnings:
        if warning not in warnings:
            warnings.append(warning)
    configured_tools = [
        tool
        for tool in configured_tools
        if not tool_disabled_by_agent_config(
            tool,
            disabled_categories=disabled_categories,
            disabled_tools=disabled_tools_set,
            disabled_mcp_servers=disabled_mcp_servers,
        )
    ]
    configured_tools = await resolve_tool_classifications(
        configured_tools,
        session_factory=request.app.state.session_factory,
        owner_email=acting_user_email,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )

    configured_items = [
        EffectiveToolItemResponse(
            tool_id=_tool_identifier(tool),
            name=tool.name,
            description=tool.description,
            category=tool.category,
            profile_group=tool.profile_group,
            read_only=tool.read_only,
            capabilities=[str(capability) for capability in tool.capabilities],
            classification_status=tool.classification_status,
            classification_source=tool.classification_source,
            classification_confidence=tool.classification_confidence,
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
        if (
            conn is not None
            and selected.runtime_state in {"active", "degraded"}
            and selected.desired_config_version == selected.applied_config_version
        ):
            connected = True
            remote_tools = await conn.list_tools()
            merged_result = await _merge_remote_runtime_inventory(
                remote_tools_data=remote_tools,
                agent_tools=configured_tools,
                providers=request.app.state.providers,
                agent=agent,
                disabled_categories=disabled_categories,
                disabled_tools=disabled_tools_set,
                disabled_mcp_servers=disabled_mcp_servers,
                intaris_result=intaris_result,
            )
            for warning in merged_result.warnings:
                if warning not in warnings:
                    warnings.append(warning)
            live_tools = await resolve_tool_classifications(
                merged_result.tools,
                session_factory=request.app.state.session_factory,
                owner_email=acting_user_email,
                queue=getattr(request.app.state, "tool_classification_queue", None),
            )
            for tool in live_tools:
                live_items.append(
                    EffectiveToolItemResponse(
                        tool_id=_tool_identifier(tool),
                        name=tool.name,
                        description=tool.description,
                        category=tool.category,
                        profile_group=tool.profile_group,
                        read_only=tool.read_only,
                        capabilities=[str(capability) for capability in tool.capabilities],
                        classification_status=tool.classification_status,
                        classification_source=tool.classification_source,
                        classification_confidence=tool.classification_confidence,
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

    # Stage 36: resolve the agent's full executor pool so the response can
    # describe primary + additional executors and per-tool availability.
    pool_executors: list[EffectiveToolsExecutorResponse] = []
    pool_observed_tool_names: dict[str, set[str]] = {}
    try:
        from cognis.core.executor_pool import resolve_executor_pool

        pool = await resolve_executor_pool(
            session_factory=session_factory,
            agent_execution=execution,
            user_email=acting_user_email,
            executor_owner_email=executor_owner_email,
            policy=policy,
        )
        for target in pool.all:
            pool_executors.append(
                EffectiveToolsExecutorResponse(
                    executor_id=target.executor_id,
                    executor_type=target.executor_type or None,
                    selection_source=target.selection_source,
                    is_primary=target.is_primary,
                    is_active=(selected is not None and target.executor_id == selected.executor_id),
                    state=target.state.value,
                    description=target.description,
                )
            )
            pool_observed_tool_names[target.executor_id] = target.observed_tool_names
    except Exception:
        # Non-fatal: degenerate to legacy single-executor shape.
        pool = None

    # Annotate tool items with the list of executors that observe each tool.
    if pool_observed_tool_names:

        def _available_on(tool_name: str) -> list[str]:
            return sorted(
                executor_id
                for executor_id, names in pool_observed_tool_names.items()
                if tool_name in names
            )

        for item in configured_items:
            item.available_on = _available_on(item.name)
        for item in live_items:
            item.available_on = _available_on(item.name)

    return EffectiveToolsResponse(
        executor=executor_summary,
        executors=pool_executors,
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


@router.get("/api/v1/intaris/mcp/tools", response_model=list[ToolResponse])
async def list_intaris_mcp_tools(request: Request) -> list[ToolResponse]:
    """List normalized Intaris MCP tools across all enabled servers."""
    user = require_current_user(request)
    guardrails = request.app.state.providers.guardrails
    aggregated = await guardrails.list_mcp_tools()

    unique: dict[str, ToolDefinition] = {}
    malformed_servers: set[str] = set()
    for row in aggregated:
        if not isinstance(row, dict):
            continue
        server_name = extract_intaris_aggregated_server_name(row)
        raw_tool_name = extract_intaris_aggregated_raw_tool_name(row)
        if server_name and not raw_tool_name:
            malformed_servers.add(server_name)
        if not server_name or not raw_tool_name:
            continue
        tool = build_intaris_tool_definition(
            server_name=server_name,
            raw_tool_name=raw_tool_name,
            payload=row,
        )
        unique.setdefault(stable_tool_id(tool), tool)

    if not aggregated or malformed_servers:
        servers = await guardrails.list_mcp_servers(enabled_only=True)
        for server in servers:
            if not isinstance(server, dict):
                continue
            server_name = server.get("name")
            if not isinstance(server_name, str) or not server_name:
                continue
            if aggregated and server_name not in malformed_servers:
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
                tool = build_intaris_tool_definition(
                    server_name=server_name,
                    raw_tool_name=raw_tool_name,
                    payload=raw_tool,
                )
                unique.setdefault(stable_tool_id(tool), tool)

    ordered = sorted(
        unique.values(),
        key=lambda tool: (
            tool.source.server_name or "",
            tool.source.raw_tool_name or tool.name,
            tool.name,
        ),
    )
    ordered = await resolve_tool_classifications(
        ordered,
        session_factory=request.app.state.session_factory,
        owner_email=user.email,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )
    return [tool_to_response(tool) for tool in ordered]


@router.get("/api/v1/agents/{agent_id}/tools", response_model=list[ToolResponse])
async def list_agent_tools(request: Request, agent_id: str) -> list[ToolResponse]:
    async with request.app.state.session_factory() as session:
        agent = await get_agent(session, agent_id)
    if agent is None:
        raise api_exception(404, "not_found", "Agent not found")
    access = await check_agent_access(request, agent, required="view")
    access_context = RuntimeAccessContext(
        user_email=access.user.email,
        agent_id=agent.agent_id,
        agent_owner_email=agent.owner_email,
        agent_type=agent.agent_type,
    )
    kb_enabled = bool(getattr(request.app.state, "knowledgebase_enabled", False))
    classified_static = await resolve_tool_classifications(
        select_static_tools(agent, access_context=access_context, knowledgebase_enabled=kb_enabled),
        session_factory=request.app.state.session_factory,
        owner_email=agent.owner_email,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )
    tools = [tool_to_response(tool) for tool in classified_static]
    agent_definition = AgentDefinition.model_validate(agent_to_response(agent).model_dump())
    try:
        discovery_owner = (
            access.user.email if access.executor_scope == "grantee_executor" else agent.owner_email
        )
        discovered = await _discover_local_mcp_tools(request, agent_definition, discovery_owner)
    except Exception:
        discovered = []
    discovered = await resolve_tool_classifications(
        discovered,
        session_factory=request.app.state.session_factory,
        owner_email=agent.owner_email,
        queue=getattr(request.app.state, "tool_classification_queue", None),
    )
    tools.extend(
        ToolResponse.model_validate(tool_to_response(tool).model_dump()) for tool in discovered
    )
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
    access = await check_agent_access(request, agent, required="view")

    agent_definition = AgentDefinition.model_validate(agent_to_response(agent).model_dump())
    raw_servers = []
    if isinstance(agent_definition.tools, dict):
        raw_servers = agent_definition.tools.get("mcp_servers", [])
    if not isinstance(raw_servers, list) or not raw_servers:
        raise api_exception(400, "validation_error", "Agent has no local MCP servers configured")

    started_at = monotonic()
    try:
        discovery_owner = (
            access.user.email if access.executor_scope == "grantee_executor" else agent.owner_email
        )
        discovered = await asyncio.wait_for(
            _discover_local_mcp_tools(request, agent_definition, discovery_owner), timeout=30
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
        server_name = tool.source.server_name or "mcp"
        grouped.setdefault(server_name, []).append(tool.name)

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

_SECRET_PATTERNS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "AUTHORIZATION"}


def _redact_secret_mapping(values: dict[str, str] | None) -> dict[str, str]:
    """Redact mapping values that look like secrets.

    ``$secret:NAME`` references are preserved as-is because they contain
    only the credential store reference name, not the actual secret value.
    The UI needs the prefix to distinguish "Credential store" entries from
    literal values on reload.
    """
    if not values:
        return {}
    redacted: dict[str, str] = {}
    for key, value in values.items():
        if value.startswith("$secret:"):
            redacted[key] = value
        elif any(pat in key.upper() for pat in _SECRET_PATTERNS):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


def _mcp_row_to_response(row: Any) -> dict[str, Any]:
    """Convert an MCPServerRow to a response dict with secret redaction."""
    from cognis.api.models import MCPServerConfigResponse as MCPResp

    invalid_reason = invalid_mcp_config_reason(
        transport=row.transport,
        command=row.command,
        url=row.url,
        env=row.env,
        headers=row.headers,
        auth_config=row.auth_config,
    )
    auth_config = effective_mcp_auth_config(row.auth_config, row.headers).model_dump()
    if auth_config.get("client_secret_ref") and not str(
        auth_config["client_secret_ref"]
    ).startswith("$secret:"):
        auth_config["client_secret_ref"] = "***"
    if isinstance(auth_config.get("authorization_params"), dict):
        auth_config["authorization_params"] = _redact_secret_mapping(
            {
                str(key): str(value)
                for key, value in auth_config["authorization_params"].items()
                if value is not None
            }
        )

    return MCPResp(
        server_id=row.server_id,
        name=row.name,
        transport=row.transport,
        command=row.command,
        url=row.url,
        args=row.args or [],
        env=_redact_secret_mapping(row.env),
        headers=_redact_secret_mapping(row.headers),
        auth_config=auth_config,
        timeout_seconds=row.timeout_seconds,
        description=row.description,
        shared=_mcp_is_shared(row),
        owner_email=row.owner_email,
        status=row.status,
        invalid_reason=invalid_reason,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    ).model_dump()


@router.get("/api/v1/mcp-servers")
async def list_mcp_servers_route(request: Request) -> list[dict[str, Any]]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_global_mcp_servers(session, owner_email=user.email, include_shared=True)
    return [_mcp_row_to_response(r) for r in rows]


@router.get("/api/v1/mcp-servers/{server_id}")
async def get_mcp_server_route(request: Request, server_id: str) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_mcp_server(session, server_id, owner_email=user.email, include_shared=True)
    if row is None:
        raise api_exception(404, "not_found", "MCP server not found")
    return _mcp_row_to_response(row)


@router.post("/api/v1/mcp-servers")
async def create_mcp_server_route(request: Request, body: MCPServerCreateRequest) -> dict[str, Any]:
    user = require_current_user(request)
    if body.shared and user.role != "admin":
        raise api_exception(403, "forbidden", "Only admins can create shared MCP servers")
    # Normalize args: split any whitespace-containing entries so that
    # "npx -y @doist/todoist-ai" stored as a single arg becomes ["-y", "@doist/todoist-ai"].
    normalized_args = _normalize_mcp_args(body.args)
    try:
        headers = canonicalize_mcp_headers(body.headers)
    except ValueError as exc:
        raise api_exception(422, "validation_error", str(exc)) from exc
    async with request.app.state.session_factory() as session:
        row = await create_mcp_server(
            session,
            server_id=body.server_id,
            name=body.name,
            transport=body.transport,
            command=body.command,
            url=body.url,
            args=normalized_args,
            env=body.env,
            headers=headers,
            auth_config=body.auth_config,
            timeout_seconds=body.timeout_seconds,
            description=body.description,
            owner_email=user.email,
            shared=body.shared,
        )
        await session.commit()
    return _mcp_row_to_response(row)


@router.put("/api/v1/mcp-servers/{server_id}")
async def update_mcp_server_route(
    request: Request, server_id: str, body: MCPServerUpdateRequest
) -> dict[str, Any]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        existing = await get_mcp_server(
            session, server_id, owner_email=user.email, include_shared=True
        )
        if existing is None:
            raise api_exception(404, "not_found", "MCP server not found")
        if _mcp_is_shared(existing) and user.role != "admin":
            raise api_exception(403, "forbidden", "Only admins can modify shared MCP servers")
        updates = body.model_dump(exclude_unset=True)
        if updates.get("shared") and user.role != "admin":
            raise api_exception(403, "forbidden", "Only admins can create shared MCP servers")
        if "args" in updates and isinstance(updates["args"], list):
            updates["args"] = _normalize_mcp_args(updates["args"])
        if "headers" in updates and isinstance(updates["headers"], dict):
            try:
                updates["headers"] = canonicalize_mcp_headers(updates["headers"])
            except ValueError as exc:
                raise api_exception(422, "validation_error", str(exc)) from exc
        if isinstance(updates.get("env"), dict) and isinstance(existing.env, dict):
            preserved_env: dict[str, str] = {}
            for key, value in updates["env"].items():
                preserved_env[key] = (
                    str(existing.env.get(key, "")) if value == "***" else str(value)
                )
            updates["env"] = preserved_env
        if isinstance(updates.get("headers"), dict) and isinstance(existing.headers, dict):
            preserved_headers: dict[str, str] = {}
            for key, value in updates["headers"].items():
                preserved_headers[key] = (
                    str(existing.headers.get(key, "")) if value == "***" else str(value)
                )
            updates["headers"] = preserved_headers
        merged = {
            "name": updates.get("name", existing.name),
            "transport": updates.get("transport", existing.transport),
            "command": updates.get("command", existing.command),
            "url": updates.get("url", existing.url),
            "args": updates.get("args", existing.args or []),
            "env": updates.get("env", existing.env or {}),
            "headers": updates.get("headers", existing.headers or {}),
            "auth_config": updates.get("auth_config", existing.auth_config),
            "timeout_seconds": updates.get("timeout_seconds", existing.timeout_seconds),
        }
        try:
            MCPServerConfig.model_validate(merged)
        except ValidationError as exc:
            raise api_exception(422, "validation_error", str(exc)) from exc
        row = await update_mcp_server(
            session,
            server_id,
            owner_email=user.email,
            include_shared=True,
            **updates,
        )
        if row is None:
            raise api_exception(404, "not_found", "MCP server not found")
        await session.commit()
        await schedule_mcp_server_executor_reconfigure_for_app(
            request.app,
            server_id=row.server_id,
            reason="mcp_server_update",
        )
    return _mcp_row_to_response(row)


@router.delete("/api/v1/mcp-servers/{server_id}")
async def delete_mcp_server_route(request: Request, server_id: str) -> dict[str, str]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        existing = await get_mcp_server(
            session, server_id, owner_email=user.email, include_shared=True
        )
        if existing is None:
            raise api_exception(404, "not_found", "MCP server not found")
        if _mcp_is_shared(existing) and user.role != "admin":
            raise api_exception(403, "forbidden", "Only admins can delete shared MCP servers")
        # Check for executor references before deleting
        referencing = await mcp_server_referenced_by_executors(
            session,
            server_id,
            owner_email=user.email,
            include_shared=True,
        )
        if referencing:
            raise api_exception(
                409,
                "referenced",
                f"MCP server is referenced by executor(s): {', '.join(referencing)}. "
                "Remove the assignment first.",
            )
        deleted = await delete_mcp_server(
            session,
            server_id,
            owner_email=user.email,
            include_shared=True,
        )
        if not deleted:
            raise api_exception(404, "not_found", "MCP server not found")
        await session.commit()
    return {"status": "deleted"}


@router.post(
    "/api/v1/tools/classification/requeue",
    response_model=ToolClassificationActionResponse,
)
async def requeue_tool_classification(
    request: Request,
    payload: ToolClassificationRequeueRequest,
) -> ToolClassificationActionResponse:
    user = require_current_user(request)
    now = _utcnow()
    updated = 0
    async with request.app.state.session_factory() as session:
        if payload.tool_id:
            row = (
                (
                    await session.execute(
                        select(ToolClassificationRow).where(
                            ToolClassificationRow.tool_id == payload.tool_id,
                            ToolClassificationRow.scope_key == user.email,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise api_exception(404, "not_found", "Tool classification not found")
            rows = [row]
        else:
            query = select(ToolClassificationRow).where(
                ToolClassificationRow.scope_key == user.email
            )
            if payload.pending_only:
                query = query.where(ToolClassificationRow.status == "pending")
            rows = list((await session.execute(query)).scalars().all())
        for row in rows:
            row.status = "pending"
            row.next_retry_at = now
            row.last_error = None
            row.attempts = 0
            row.category = None
            row.capabilities = None
            row.classification_source = None
            row.classification_confidence = None
        updated = len(rows)
        await session.commit()
    if updated:
        request.app.state.tool_classification_queue._wake_event.set()
    return ToolClassificationActionResponse(updated=updated, status="queued")


@router.put(
    "/api/v1/tools/classification/override",
    response_model=ToolClassificationActionResponse,
)
async def override_tool_classification(
    request: Request,
    payload: ToolClassificationOverrideRequest,
) -> ToolClassificationActionResponse:
    user = require_current_user(request)
    if payload.profile_group not in AUTO_PROFILE_GROUPS:
        raise api_exception(400, "validation_error", "Invalid profile_group")
    try:
        capabilities = [str(ToolCapability(item)) for item in payload.capabilities]
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    if not capabilities:
        raise api_exception(400, "validation_error", "At least one capability is required")
    async with request.app.state.session_factory() as session:
        await upsert_tool_classification_override(
            session,
            scope_key=user.email,
            owner_email=user.email,
            tool_id=payload.tool_id,
            profile_group=payload.profile_group,
            capabilities=capabilities,
        )
        await session.commit()
    return ToolClassificationActionResponse(updated=1, status="override_saved")


@router.post(
    "/api/v1/tools/classification/reset-override",
    response_model=ToolClassificationActionResponse,
)
async def reset_tool_classification_override(
    request: Request,
    payload: ToolClassificationRequeueRequest,
) -> ToolClassificationActionResponse:
    user = require_current_user(request)
    if not payload.tool_id:
        raise api_exception(400, "validation_error", "tool_id is required")
    row = await _get_classification_override_for_tool(
        request, user_email=user.email, tool_id=payload.tool_id
    )
    if row is None:
        raise api_exception(404, "not_found", "Tool classification not found")
    async with request.app.state.session_factory() as session:
        await delete_tool_classification_override(
            session,
            scope_key=user.email,
            tool_id=payload.tool_id,
        )
        await session.commit()
    auto_row = await _get_classification_row_for_tool(
        request, user_email=user.email, tool_id=payload.tool_id
    )
    if auto_row is not None:
        async with request.app.state.session_factory() as session:
            fresh = await session.get(ToolClassificationRow, auto_row.classification_id)
            assert fresh is not None
            fresh.status = "pending"
            fresh.attempts = 0
            fresh.next_retry_at = _utcnow()
            fresh.last_error = None
            await session.commit()
        request.app.state.tool_classification_queue._wake_event.set()
    return ToolClassificationActionResponse(updated=1, status="override_reset")


def _normalize_mcp_args(args: list[str] | None) -> list[str]:
    """Split whitespace-containing arg entries into individual tokens.

    Users often paste ``-y @doist/todoist-ai`` as a single argument.
    ``create_subprocess_exec`` treats each list element as one argv entry,
    so the space-containing string is passed verbatim and breaks the
    spawned process.  This normalizer splits such entries so the executor
    receives clean individual tokens.
    """
    if not args:
        return []
    normalized: list[str] = []
    for arg in args:
        parts = arg.strip().split()
        normalized.extend(p for p in parts if p)
    return normalized
