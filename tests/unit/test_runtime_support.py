from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import cognis.store.queries as store_queries
from cognis.api import runtime_support
from cognis.api.runtime_support import (
    _build_remote_runtime_registry,
    _mcp_degraded_notice,
    _merge_remote_runtime_inventory,
    _resolve_intaris_mcp_tools,
    mcp_server_assignment_key,
    select_static_tools,
    tool_disabled_by_agent_config,
)
from cognis.core.executor_pin_lifecycle import ExecutorPinLifecycleResult
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.executor_pool import ExecutorPool
from cognis.core.runtime import TransientExecutorUnavailable
from cognis.core.tool_router import ToolRoute, ToolRouter
from cognis.core.turn_timing import isolate_turn_timing
from cognis.models.agent import AgentDefinition
from cognis.models.knowledgebase import KnowledgebaseModel
from cognis.models.session import SessionModel
from cognis.models.skill import ResolvedSkill, SkillToolSpec
from cognis.models.tool import (
    ExecutorHandle,
    ToolCall,
    ToolSource,
    sanitize_mcp_tool_name,
)
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.providers.executor.in_process import InProcessExecutorConnection
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import (
    Agent,
    Base,
    Conversation,
    ExecutorPinNoticeOutboxRow,
    ExecutorPinTransitionRow,
    Task,
    User,
)
from cognis.tools.builtin.agent_management import MANAGE_AGENTS_TOOL
from cognis.tools.registry import RegisteredTool, ToolExecutionContext, ToolRegistry
from cognis.tools.skills import ResolvedSkillSet


async def _executor_pin_lifecycle_settings(_session_factory: object) -> dict[str, int]:
    return {"retry_seconds": 0, "retry_interval_seconds": 0}


@pytest.fixture
def runtime_timing_records(monkeypatch):
    records = []

    def capture(message, *, extra):
        assert message == "turn: stage completed"
        records.append(extra["extra_data"])

    monkeypatch.setattr("cognis.core.turn_timing.logger.info", capture)
    return records


@pytest.mark.asyncio
@pytest.mark.parametrize("program_enabled", [False, True])
@pytest.mark.parametrize("pool_failed", [False, True])
async def test_prepare_only_runtime_retains_observed_tools_without_side_effects(
    monkeypatch, program_enabled, pool_failed, runtime_timing_records
):
    from unittest.mock import AsyncMock

    from cognis.core.program_config import PROGRAM_RUNTIME_TOOL_NAMES
    from cognis.tools.executor.definitions import ALL_EXECUTOR_TOOLS

    observed = ToolDefinition(
        name="read",
        description="Read a file.",
        parameters={"type": "object"},
        source=ToolSource(type="executor"),
        read_only=True,
        category="filesystem",
    )
    resolve_config = AsyncMock(
        return_value={
            "executor_id": "alice_exec",
            "executor_type": "websocket",
            "config": {"program_runtime": {"host_enabled": program_enabled}},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "selection_source": "explicit",
            "runtime_state": "active",
            "observed_tools": [
                observed.model_dump(mode="json"),
                *[
                    tool.model_dump(mode="json")
                    for tool in ALL_EXECUTOR_TOOLS
                    if tool.name in PROGRAM_RUNTIME_TOOL_NAMES
                ],
            ],
        }
    )
    monkeypatch.setattr(
        runtime_support, "load_executor_policy", AsyncMock(return_value=ExecutorPolicy())
    )
    monkeypatch.setattr(runtime_support, "_resolve_eligible_executor_config", resolve_config)
    monkeypatch.setattr(
        runtime_support,
        "_resolve_web_config",
        AsyncMock(return_value={"web_available_backends": ["direct"], "web_backend": "direct"}),
    )
    monkeypatch.setattr(
        runtime_support, "resolve_skills_for_agent", AsyncMock(return_value=ResolvedSkillSet())
    )
    pool = ExecutorPool()
    monkeypatch.setattr(
        "cognis.core.executor_pool.resolve_executor_pool",
        AsyncMock(
            return_value=pool,
            side_effect=RuntimeError("pool resolution unavailable") if pool_failed else None,
        ),
    )
    dispatcher = SimpleNamespace(dispatch_pending=AsyncMock())
    executor = SimpleNamespace(
        spawn=AsyncMock(),
        websocket=SimpleNamespace(
            get_connection=AsyncMock(),
            list_tools=AsyncMock(),
        ),
    )
    providers = _runtime_providers_with_executor(executor)
    providers.executor_pin_notice_dispatcher = dispatcher
    factory = runtime_support.build_step_runtime_factory(
        providers=providers,
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=_runtime_session_factory,
    )
    agent = _agent()
    agent.owner_email = "alice@example.com"

    @isolate_turn_timing
    async def resolve(*, session, turn_id):
        return await factory(agent, agent.owner_email, prepare_only=True)

    runtime = await resolve(
        session=SimpleNamespace(session_id="session-runtime-timing"),
        turn_id="turn-runtime-timing",
    )
    assert runtime.tool_registry.get("read") is not None
    for name in PROGRAM_RUNTIME_TOOL_NAMES:
        assert (runtime.tool_registry.get(name) is not None) is program_enabled
    assert resolve_config.call_args.kwargs["read_only"] is True
    assert runtime.executor_connection is None
    assert runtime.executor_pool is (None if pool_failed else pool)
    dispatcher.dispatch_pending.assert_not_awaited()
    executor.spawn.assert_not_awaited()
    executor.websocket.get_connection.assert_not_awaited()
    executor.websocket.list_tools.assert_not_awaited()
    assert [record["stage"] for record in runtime_timing_records] == [
        "runtime.policy_load",
        "runtime.scope_resolve",
        "runtime.executor_select",
        "runtime.pool_resolve",
        "runtime.web_config",
        "runtime.image_tool_descriptions",
        "runtime.skills_resolve",
        "runtime.build_attempt",
        "turn.total",
    ]
    for record in runtime_timing_records:
        assert record["session_id"] == "session-runtime-timing"
        assert record["turn_id"] == "turn-runtime-timing"
        assert record["outcome"] == (
            "interrupted"
            if pool_failed and record["stage"] == "runtime.pool_resolve"
            else "completed"
        )
        assert record["duration_seconds"] >= 0
        assert set(record) == {"session_id", "turn_id", "stage", "duration_seconds", "outcome"}
    await runtime.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_runtime_timing_preserves_policy_failure_and_cancellation(
    monkeypatch, runtime_timing_records, error_type
):
    from unittest.mock import AsyncMock

    failure = error_type("sensitive policy failure")
    monkeypatch.setattr(runtime_support, "load_executor_policy", AsyncMock(side_effect=failure))
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers(),
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=_runtime_session_factory,
    )
    with pytest.raises(error_type) as caught:
        await factory(_agent(), "user@example.com", prepare_only=True)
    assert caught.value is failure
    assert [record["stage"] for record in runtime_timing_records] == [
        "runtime.policy_load",
        "runtime.build_attempt",
    ]
    assert all(record["outcome"] == "interrupted" for record in runtime_timing_records)
    assert "sensitive" not in repr(runtime_timing_records)
    assert "user@example.com" not in repr(runtime_timing_records)


def test_mcp_degraded_notice_exposes_only_authorization_failures() -> None:
    notice = _mcp_degraded_notice(
        {
            "mcp_servers": [
                {
                    "name": "mfg-portal",
                    "server_id": "mcp_mfg_portal",
                    "status": "failed",
                    "authorization_required": True,
                },
                {"name": "unrelated", "status": "failed", "authorization_required": False},
                {"name": "healthy", "status": "ready"},
            ]
        },
        can_authorize=True,
    )

    assert notice is not None
    assert "mfg-portal" in notice
    assert "mcp_mfg_portal" in notice
    assert "unrelated" not in notice
    assert "authorization required" in notice
    assert "manage_mcp" in notice


def test_mcp_degraded_notice_ignores_missing_or_non_authorization_metadata() -> None:
    assert _mcp_degraded_notice({}, can_authorize=True) is None
    assert (
        _mcp_degraded_notice(
            {"mcp_servers": [{"name": "mfg-portal", "status": "failed"}]},
            can_authorize=True,
        )
        is None
    )


def test_mcp_degraded_notice_directs_user_when_management_tool_is_unavailable() -> None:
    notice = _mcp_degraded_notice(
        {
            "mcp_servers": [
                {
                    "name": "mfg-portal",
                    "server_id": "mcp_mfg_portal",
                    "status": "failed",
                    "authorization_required": True,
                }
            ]
        },
        can_authorize=False,
    )

    assert notice is not None
    assert "Tools & Skills" in notice
    assert "manage_mcp" not in notice


def _agent(*, tools: dict[str, object] | None = None) -> AgentDefinition:
    return AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools=tools or {},
    )


def _builtin_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="system",
        read_only=True,
    )


def _mcp_tool(*, name: str, server_name: str, server_id: str | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            type="local_mcp",
            server_name=server_name,
            server_id=server_id,
            raw_tool_name=name,
        ),
        category="mcp",
        read_only=True,
    )


def test_mcp_server_assignment_key_prefers_stable_server_id() -> None:
    tool = _mcp_tool(name="search", server_name="github", server_id="srv-github")

    assert mcp_server_assignment_key(tool.source) == "local_mcp:srv-github"


def test_legacy_mcp_disabled_category_does_not_disable_mcp_tools() -> None:
    tool = _mcp_tool(name="search", server_name="github", server_id="srv-github")

    assert not tool_disabled_by_agent_config(
        tool,
        disabled_categories={"mcp"},
        disabled_tools=set(),
        disabled_mcp_servers=set(),
    )


def test_mcp_server_disable_still_disables_mcp_tools() -> None:
    tool = _mcp_tool(name="search", server_name="github", server_id="srv-github")

    assert tool_disabled_by_agent_config(
        tool,
        disabled_categories=set(),
        disabled_tools=set(),
        disabled_mcp_servers={"local_mcp:srv-github"},
    )


def test_manage_agents_is_default_off_without_opt_in() -> None:
    agent = AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent")

    selected = {tool.name for tool in select_static_tools(agent)}

    assert "manage_agents" not in selected


def test_manage_agents_requires_opt_in_even_with_wildcard_builtin_tools() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"builtin_tools": ["*"]},
    )

    selected = {tool.name for tool in select_static_tools(agent)}

    assert "manage_agents" not in selected


def test_manage_agents_requires_explicit_runtime_context() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"opt_in_builtin_tools": ["manage_agents"]},
    )

    selected = {tool.name for tool in select_static_tools(agent)}

    assert "manage_agents" not in selected


def test_manage_agents_hidden_from_global_static_selection() -> None:
    selected = {tool.name for tool in select_static_tools()}

    assert "manage_agents" not in selected


def test_manage_agents_exposed_for_root_owner_primary_context() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"opt_in_builtin_tools": ["manage_agents"]},
    )
    access_context = RuntimeAccessContext(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
    )

    selected = {tool.name for tool in select_static_tools(agent, access_context=access_context)}

    assert "manage_agents" in selected


def test_manage_agents_opt_in_bypasses_narrow_builtin_allowlist() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"builtin_tools": ["list_agents"], "opt_in_builtin_tools": ["manage_agents"]},
    )
    access_context = RuntimeAccessContext(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
    )

    selected = {tool.name for tool in select_static_tools(agent, access_context=access_context)}

    assert "manage_agents" in selected


def test_agent_tool_group_allows_knowledgebase_read_tools() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"builtin_tools": [], "tool_groups": ["knowledgebase_read"]},
    )

    selected = {
        tool.name
        for tool in select_static_tools(agent, knowledgebase_enabled=True)
        if tool.category == "knowledgebase"
    }

    assert "knowledgebase_search" in selected
    assert "knowledgebase_read_source_context" in selected
    assert "knowledgebase_delete" not in selected


def test_agent_tool_deny_overrides_tool_group() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={
            "builtin_tools": [],
            "tool_groups": ["knowledgebase_read"],
            "deny_tools": ["builtin:knowledgebase_status"],
        },
    )

    selected = {
        tool.name
        for tool in select_static_tools(agent, knowledgebase_enabled=True)
        if tool.category == "knowledgebase"
    }

    assert "knowledgebase_status" not in selected
    assert "knowledgebase_search" in selected


def test_manage_agents_hidden_for_shared_grantee_context() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="owner@example.com",
        name="Agent",
        tools={"opt_in_builtin_tools": ["manage_agents"]},
    )
    access_context = RuntimeAccessContext(
        user_email="guest@example.com",
        agent_id="agent-1",
        agent_owner_email="owner@example.com",
    )

    selected = {tool.name for tool in select_static_tools(agent, access_context=access_context)}

    assert "manage_agents" not in selected


def test_manage_agents_hidden_for_delegated_context() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"opt_in_builtin_tools": ["manage_agents"]},
    )
    access_context = RuntimeAccessContext(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        parent_session_id="parent",
        delegation_mode="worker",
    )

    selected = {tool.name for tool in select_static_tools(agent, access_context=access_context)}

    assert "manage_agents" not in selected


def test_manage_agents_hidden_for_unknown_agent_type() -> None:
    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="user@example.com",
        name="Agent",
        tools={"opt_in_builtin_tools": ["manage_agents"]},
    )
    access_context = RuntimeAccessContext(
        user_email="user@example.com",
        agent_id="agent-1",
        agent_owner_email="user@example.com",
        agent_type="custom",
    )

    selected = {tool.name for tool in select_static_tools(agent, access_context=access_context)}

    assert "manage_agents" not in selected


def test_manage_agents_name_is_reserved_against_executor_shadowing() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            definition=ToolDefinition(
                name="manage_agents",
                description="shadow",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
            )
        )
    )
    router = ToolRouter(guardrails=object())

    assert router.classify("manage_agents", registry) is ToolRoute.UNKNOWN


@pytest.mark.asyncio
async def test_manage_agents_execution_rechecks_explicit_opt_in() -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MANAGE_AGENTS_TOOL))
    router = ToolRouter(guardrails=object())

    result = await router.execute(
        ToolCall(
            call_id="call-1",
            name="manage_agents",
            arguments={"action": "list"},
            runtime_metadata={
                "runtime_access": {
                    "user_email": "user@example.com",
                    "agent_id": "agent-1",
                    "agent_owner_email": "user@example.com",
                    "agent_type": "primary",
                }
            },
        ),
        SessionModel(
            session_id="s", conversation_id="c", user_email="user@example.com", agent_id="agent-1"
        ),
        AgentDefinition(agent_id="agent-1", owner_email="user@example.com", name="Agent"),
        registry,
        executor=object(),
    )

    assert result.is_error is True
    assert result.metadata["code"] == "agent_management_not_enabled"


@pytest.mark.asyncio
async def test_manage_agents_execution_validates_arguments_before_guardrails() -> None:
    registry = ToolRegistry()
    registry.register(RegisteredTool(definition=MANAGE_AGENTS_TOOL))
    router = ToolRouter(guardrails=object())

    result = await router.execute(
        ToolCall(
            call_id="call-1",
            name="manage_agents",
            arguments={"_raw": "not json"},
            runtime_metadata={
                "runtime_access": {
                    "user_email": "user@example.com",
                    "agent_id": "agent-1",
                    "agent_owner_email": "user@example.com",
                    "agent_type": "primary",
                }
            },
        ),
        SessionModel(
            session_id="s",
            conversation_id="c",
            user_email="user@example.com",
            agent_id="agent-1",
        ),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="user@example.com",
            name="Agent",
            tools={"opt_in_builtin_tools": ["manage_agents"]},
        ),
        registry,
        executor=object(),
    )

    assert result.is_error is True
    assert result.metadata["code"] == "invalid_tool_arguments"
    assert "invalid_tool_arguments" in result.output


class _Guardrails:
    def __init__(
        self,
        *,
        aggregated: list[dict[str, object]] | None = None,
        servers: list[dict[str, object]] | None = None,
        fail_aggregated: bool = False,
    ) -> None:
        self.aggregated = aggregated or []
        self.servers = servers or []
        self.fail_aggregated = fail_aggregated
        self.server_calls = 0

    async def list_mcp_tools(self) -> list[dict[str, object]]:
        if self.fail_aggregated:
            raise RuntimeError("boom")
        return self.aggregated

    async def list_mcp_servers(self, enabled_only: bool = True) -> list[dict[str, object]]:
        assert enabled_only is True
        self.server_calls += 1
        return self.servers


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_prefers_aggregated_listing() -> None:
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ]
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        set(),
    )

    assert result.fallback_used is False
    assert result.warnings == []
    assert len(result.tools) == 1
    tool = result.tools[0]
    assert tool.name == sanitize_mcp_tool_name("github", "search/issues")
    assert tool.source.server_name == "github"
    assert tool.source.raw_tool_name == "search/issues"


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_accepts_name_field_from_intaris() -> None:
    """Intaris GET /mcp/tools returns 'name' (not 'tool' or 'raw_tool_name').

    The extractor must accept 'name' as a valid tool name field so the
    aggregated listing is used directly without falling back to server cache.
    """
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "name": "search/issues",
                    "description": "Search issues",
                    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ]
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        set(),
    )

    assert result.fallback_used is False
    assert result.warnings == []
    assert len(result.tools) == 1
    tool = result.tools[0]
    assert tool.name == sanitize_mcp_tool_name("github", "search/issues")
    assert tool.source.server_name == "github"
    assert tool.source.raw_tool_name == "search/issues"


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_falls_back_per_missing_server() -> None:
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "server": "linear",
                    "description": "Missing metadata row should be skipped",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
            servers=[
                {
                    "name": "linear",
                    "tools_cache": [
                        {
                            "name": "search/issues",
                            "description": "Linear fallback",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ],
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github", "linear"]}),
        set(),
        set(),
    )

    assert result.fallback_used is True
    assert len(result.tools) == 2
    assert {tool.source.server_name for tool in result.tools} == {"github", "linear"}
    assert any("fallback" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_does_not_fallback_without_cached_server_manifest() -> None:
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            servers=[
                {
                    "name": "linear",
                    "tools_cache": [],
                }
            ],
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github", "linear"]}),
        set(),
        set(),
    )

    assert result.fallback_used is False
    assert {tool.source.server_name for tool in result.tools} == {"github"}
    assert any("no cached manifest fallback" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_falls_back_for_assigned_server_omitted_from_aggregate() -> (
    None
):
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            servers=[
                {
                    "name": "linear",
                    "tools_cache": [
                        {
                            "name": "search/issues",
                            "description": "Linear cached tool",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ],
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github", "linear"]}),
        set(),
        set(),
    )

    assert result.fallback_used is True
    assert {tool.source.server_name for tool in result.tools} == {"github", "linear"}


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_falls_back_when_aggregated_listing_is_empty() -> None:
    guardrails = _Guardrails(
        aggregated=[],
        servers=[
            {
                "name": "github",
                "tools_cache": [
                    {
                        "name": "search/issues",
                        "description": "Cached fallback tool",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ],
            }
        ],
    )
    providers = SimpleNamespace(guardrails=guardrails)

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        set(),
    )

    assert result.fallback_used is True
    assert len(result.tools) == 1
    assert result.tools[0].source.server_name == "github"
    assert any("empty" in warning.lower() for warning in result.warnings)
    assert guardrails.server_calls == 1


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_empty_aggregate_without_cache_only_checks_once() -> None:
    guardrails = _Guardrails(
        aggregated=[],
        servers=[{"name": "github", "tools_cache": []}],
    )
    providers = SimpleNamespace(guardrails=guardrails)

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        set(),
    )

    assert result.tools == []
    assert result.fallback_used is False
    assert guardrails.server_calls == 1
    assert any("empty" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_recovers_missing_tools_for_partially_malformed_server() -> (
    None
):
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Aggregated tool",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "server": "github",
                    "description": "Malformed missing raw tool name",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
            servers=[
                {
                    "name": "github",
                    "tools_cache": [
                        {
                            "name": "search/issues",
                            "description": "Cached duplicate",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                        {
                            "name": "search/projects",
                            "description": "Cached recovery tool",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                    ],
                }
            ],
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        set(),
    )

    assert result.fallback_used is True
    assert {tool.source.raw_tool_name for tool in result.tools} == {
        "search/issues",
        "search/projects",
    }


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_does_not_fallback_when_filtered_by_agent() -> None:
    raw_tool = "search/issues"
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": raw_tool,
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            servers=[
                {
                    "name": "github",
                    "tools_cache": [
                        {
                            "name": raw_tool,
                            "description": "Should stay filtered",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            ],
        )
    )

    disabled = {sanitize_mcp_tool_name("github", raw_tool)}
    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        disabled,
    )

    assert result.tools == []
    assert result.fallback_used is False


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_suffixes_actual_normalized_name_collisions() -> None:
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "server": "github",
                    "tool": "search_issues",
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github"]}),
        set(),
        set(),
    )

    assert {tool.name for tool in result.tools} == {
        "mcp_github__search_issues_9287b261",
        "mcp_github__search_issues_28fc1708",
    }


@pytest.mark.asyncio
async def test_merge_remote_runtime_inventory_prefers_remote_and_builtin_on_collisions() -> None:
    colliding_name = sanitize_mcp_tool_name("github", "search/issues")
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search/issues",
                    "description": "Search issues",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        )
    )

    result = await _merge_remote_runtime_inventory(
        remote_tools_data=[
            ToolDefinition(
                name=colliding_name,
                description="Remote tool",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                category="mcp",
                read_only=True,
                timeout_seconds=30,
                non_bypassable=False,
            ).model_dump(mode="json")
        ],
        agent_tools=[_builtin_tool(colliding_name), _builtin_tool("list_agents")],
        providers=providers,
        agent=_agent(tools={"intaris_mcp_servers": ["github"]}),
        disabled_categories=set(),
        disabled_tools=set(),
    )

    assert [tool.name for tool in result.tools] == [colliding_name, "list_agents"]
    assert result.collision_count >= 2
    assert any("hidden" in warning.lower() for warning in result.warnings)


@pytest.mark.asyncio
async def test_merge_remote_runtime_inventory_honors_disabled_mcp_server_group() -> None:
    disabled_tool = _mcp_tool(
        name="mcp_github__search",
        server_name="github",
        server_id="srv-github",
    )
    enabled_tool = _mcp_tool(
        name="mcp_linear__search",
        server_name="linear",
        server_id="srv-linear",
    )
    providers = SimpleNamespace(guardrails=_Guardrails(aggregated=[]))

    result = await _merge_remote_runtime_inventory(
        remote_tools_data=[
            disabled_tool.model_dump(mode="json"),
            enabled_tool.model_dump(mode="json"),
        ],
        agent_tools=[],
        providers=providers,
        agent=_agent(),
        disabled_categories=set(),
        disabled_tools=set(),
        disabled_mcp_servers={"local_mcp:srv-github"},
    )

    assert [tool.name for tool in result.tools] == ["mcp_linear__search"]


@pytest.mark.asyncio
async def test_resolve_intaris_mcp_tools_honors_disabled_mcp_server_group() -> None:
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "github",
                    "tool": "search",
                    "description": "Search GitHub",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "server": "linear",
                    "tool": "search",
                    "description": "Search Linear",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        )
    )

    result = await _resolve_intaris_mcp_tools(
        providers,
        _agent(tools={"intaris_mcp_servers": ["github", "linear"]}),
        set(),
        set(),
        {"intaris_mcp:github"},
    )

    assert {tool.source.server_name for tool in result.tools} == {"linear"}


@pytest.mark.asyncio
async def test_merge_remote_runtime_inventory_dedupes_intaris_collisions_deterministically() -> (
    None
):
    providers = SimpleNamespace(
        guardrails=_Guardrails(
            aggregated=[
                {
                    "server": "a-server",
                    "tool": "same/tool",
                    "description": "A",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "server": "a-server",
                    "tool": "same/tool",
                    "description": "B",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        )
    )

    result = await _merge_remote_runtime_inventory(
        remote_tools_data=[],
        agent_tools=[],
        providers=providers,
        agent=_agent(tools={"intaris_mcp_servers": ["a-server"]}),
        disabled_categories=set(),
        disabled_tools=set(),
    )

    assert len(result.tools) == 1
    assert result.tools[0].source.server_name == "a-server"
    assert result.collision_count == 1
    assert any("same runtime name" in warning.lower() for warning in result.warnings)


def test_build_remote_runtime_registry_attaches_only_builtin_handlers() -> None:
    async def builtin_handler(arguments: dict[str, object], context: object) -> object:
        del arguments, context
        return {"ok": True}

    registry = _build_remote_runtime_registry(
        [
            _builtin_tool("get_current_datetime"),
            ToolDefinition(
                name="read",
                description="read",
                parameters={"type": "object", "properties": {}},
                source=ToolSource(type="executor"),
                category="filesystem",
                read_only=True,
            ),
        ],
        {"get_current_datetime": builtin_handler, "read": builtin_handler},
    )

    builtin = registry.get("get_current_datetime")
    assert builtin is not None
    assert builtin.handler is builtin_handler

    executor_tool = registry.get("read")
    assert executor_tool is not None
    assert executor_tool.handler is None


class _MissingWebSocketProvider:
    def get_connection(self, executor_id: str) -> None:
        del executor_id
        return None

    def get_handle_metadata(self, executor_id: str) -> dict[str, object]:
        del executor_id
        return {}


class _ReadyWebSocketConnection:
    async def list_tools(self) -> list[dict[str, object]]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader_email", "scope_task_id", "expected"),
    [
        ("owner@example.com", "task-rich", True),
        ("other@example.com", "task-rich", False),
        ("owner@example.com", "task-owner", False),
    ],
)
async def test_remote_factory_rebuild_and_scoped_reader(
    monkeypatch,
    task_continuation_db,
    reader_email,
    scope_task_id,
    expected,
    runtime_timing_records,
) -> None:
    """Exercise real remote composition, inventory filters and controller hydration."""
    import json
    from unittest.mock import AsyncMock

    from cognis.core.program_config import PROGRAM_RUNTIME_TOOL_NAMES
    from cognis.tools.executor.definitions import ALL_EXECUTOR_TOOLS

    async def policy(_):
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def config(*_, **__):
        return {
            "executor_id": "alice_exec",
            "executor_type": "websocket",
            "config": {},
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def web(*_, **__):
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def skills(*_, **__):
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", policy)
    monkeypatch.setattr(runtime_support, "_resolve_eligible_executor_config", config)
    monkeypatch.setattr(runtime_support, "_resolve_web_config", web)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", skills)
    ws = _ReadyWebSocketProvider("alice_exec")
    ws.connection.executor_id = "alice_exec"

    async def program_policy(connection):
        return getattr(connection, "executor_id", None) == "alice_exec"

    monkeypatch.setattr(runtime_support, "ProgramPolicyResolver", lambda *args: program_policy)
    program_tools = [
        tool.model_dump(mode="json")
        for tool in ALL_EXECUTOR_TOOLS
        if tool.name in PROGRAM_RUNTIME_TOOL_NAMES
    ]

    def advertised(name, server):
        return ToolDefinition(
            name=name,
            description=name,
            parameters={"type": "object"},
            source=ToolSource(type="local_mcp", server_name=server),
            category="mcp",
            read_only=True,
        ).model_dump(mode="json")

    ws.connection.list_tools = AsyncMock(
        return_value=[advertised("calendar_search", "calendar"), *program_tools]
    )
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers_with_ws(ws),
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=task_continuation_db,
        artifact_store=task_continuation_db.artifact_store,
    )
    runtime = await factory(
        agent=AgentDefinition(
            agent_id="agent-owner",
            owner_email="owner@example.com",
            name="Agent",
            execution={"executor_id": "alice_exec"},
            tools={"disabled_mcp_servers": ["local_mcp:blocked"]},
        ),
        user_email="owner@example.com",
    )
    remote_stages = [
        record["stage"]
        for record in runtime_timing_records
        if record["stage"]
        in {"runtime.catalog_read", "runtime.inventory_merge", "runtime.registry_build"}
    ]
    assert remote_stages == [
        "runtime.catalog_read",
        "runtime.inventory_merge",
        "runtime.registry_build",
    ]
    assert all(record["outcome"] == "completed" for record in runtime_timing_records)
    replacement_conn = SimpleNamespace(
        executor_id="disabled_exec",
        list_tools=AsyncMock(
            return_value=[
                advertised("gmail_search", "gmail"),
                advertised("blocked_search", "blocked"),
                *program_tools,
            ]
        ),
    )
    # A websocket connection contains no store; only the controller handler closure does.
    router = ToolRouter.__new__(ToolRouter)
    result = await router._execute_local_handler(
        ToolCall(
            call_id="read-scoped-packet",
            name="read_task_deliverable",
            arguments={"deliverable_id": "dlv_rich"},
            runtime_metadata={
                "user_email": reader_email,
                "conversation_context": {
                    "platform_data": {
                        "forked_from": "task",
                        "task_id": scope_task_id,
                    }
                },
            },
        ),
        registered_tool=runtime.tool_registry.get("read_task_deliverable"),
        executor=replacement_conn,
    )
    payload = json.loads(result.output)
    assert payload["ok"] is expected
    if expected:
        assert payload["content"] == "Rich fallback"
        assert payload["rich_payload"]["blocks"][0]["title"] == "Finding"
    else:
        assert payload["error"] == "not_found"
    assert not hasattr(replacement_conn, "artifact_store")
    assert runtime.rebuild_tool_registry is not None
    replacement = await runtime.rebuild_tool_registry(replacement_conn)
    assert replacement.get("gmail_search") is not None
    assert replacement.get("calendar_search") is None
    assert replacement.get("blocked_search") is None
    for name in PROGRAM_RUNTIME_TOOL_NAMES:
        assert runtime.tool_registry.get(name) is not None
        assert replacement.get(name) is None
    assert runtime.tool_registry.get("calendar_search") is not None


class _ReadyWebSocketProvider:
    def __init__(self, executor_id: str) -> None:
        self.executor_id = executor_id
        self.connection = _ReadyWebSocketConnection()

    def get_connection(self, executor_id: str) -> _ReadyWebSocketConnection | None:
        return self.connection if executor_id == self.executor_id else None

    def get_handle_metadata(self, executor_id: str) -> dict[str, object]:
        assert executor_id == self.executor_id
        return {"environment": {"cwd": "/workspace", "home": "/home/alice", "user": "alice"}}


class _SecretsProvider:
    async def resolve_for_execution(self, *_: object, **__: object) -> dict[str, str]:
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "outcome"),
    [
        ("physical", "ready"),
        ("forwarded", "ready"),
        ("timeout", "ready"),
        ("forwarded", "deadline"),
        ("forwarded", "cancel"),
        ("rpc", "permanent"),
        ("forwarded_rpc", "permanent"),
        ("forwarded_protocol", "permanent"),
        ("malformed", "permanent"),
    ],
)
async def test_inventory_discovery_uses_runtime_admission_recovery(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    outcome: str,
    runtime_timing_records,
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from cognis.core.executor_recovery import ExecutorRecoveryTimeout, ExecutorRecoveryWindow
    from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError
    from cognis.providers.executor.forwarding import ForwardedDeliveryError
    from cognis.providers.executor.websocket import ExecutorRPCError

    errors = {
        "physical": ExecutorDeliveryError(
            "Executor did not reconnect for replay-safe retry",
            DeliveryState.TERMINAL,
            code="executor_recovery_timeout",
        ),
        "forwarded": ForwardedDeliveryError(
            "Executor did not reconnect for replay-safe retry",
            DeliveryState.TERMINAL,
            code="executor_recovery_timeout",
        ),
        "timeout": TimeoutError("Inventory request timed out"),
        "rpc": ExecutorRPCError(-32601, "Unknown method"),
        "forwarded_rpc": ForwardedDeliveryError(
            "Unknown method", DeliveryState.TERMINAL, code="-32601"
        ),
        "forwarded_protocol": ForwardedDeliveryError(
            "Forwarded result chunk assembly timed out", DeliveryState.TERMINAL
        ),
        "malformed": ValueError("Invalid tool inventory"),
    }
    failure = errors[failure_kind]
    executor_id = "alice_exec"
    ws = _ReadyWebSocketProvider(executor_id)
    failed_connection = ws.connection
    failed_connection.list_tools = AsyncMock(side_effect=failure)
    replacement = _ReadyWebSocketConnection()
    replacement.list_tools = AsyncMock(return_value=[])
    cancel_event = asyncio.Event()

    async def wait_for_connection(selected_id: str, **kwargs: object) -> object | None:
        assert selected_id == executor_id
        assert kwargs["cancel_event"] is cancel_event
        if outcome == "deadline":
            return None
        if outcome == "cancel":
            cancel_event.set()
            raise asyncio.CancelledError
        ws.connection = replacement
        return replacement

    waiter = AsyncMock(side_effect=wait_for_connection)
    monkeypatch.setattr(ws, "wait_for_connection", waiter, raising=False)
    monkeypatch.setattr(
        runtime_support, "load_executor_policy", AsyncMock(return_value=ExecutorPolicy())
    )
    monkeypatch.setattr(
        runtime_support,
        "_resolve_eligible_executor_config",
        AsyncMock(
            return_value={
                "executor_id": executor_id,
                "executor_type": "websocket",
                "config": {},
                "runtime_state": "active",
                "desired_config_version": 1,
                "applied_config_version": 1,
            }
        ),
    )
    monkeypatch.setattr(
        runtime_support,
        "_resolve_web_config",
        AsyncMock(return_value={"web_available_backends": ["direct"], "web_backend": "direct"}),
    )
    monkeypatch.setattr(
        runtime_support, "resolve_skills_for_agent", AsyncMock(return_value=ResolvedSkillSet())
    )
    monkeypatch.setattr(store_queries, "get_conversation", AsyncMock(return_value=None))
    now = datetime.now(UTC)
    recovery = AsyncMock(
        return_value=ExecutorRecoveryWindow(
            executor_id=executor_id,
            unavailable_since=now,
            deadline=now + timedelta(seconds=900),
            database_remaining_seconds=900,
        )
    )
    monkeypatch.setattr(runtime_support, "begin_executor_recovery", recovery)
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers_with_ws(ws),
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=_runtime_session_factory,
    )
    admission = factory(
        AgentDefinition(
            agent_id="agent-owner",
            name="Agent",
            owner_email="owner@example.com",
            execution={"executor_id": executor_id},
        ),
        "owner@example.com",
        conversation_id="conv-inventory-recovery",
        cancel_event=cancel_event,
    )
    if outcome == "ready":
        runtime = await asyncio.wait_for(admission, timeout=2)
        assert runtime.executor_connection is replacement
        assert runtime.active_executor_id == executor_id
        replacement.list_tools.assert_awaited_once()
    else:
        error_type = (
            ExecutorRecoveryTimeout
            if outcome == "deadline"
            else asyncio.CancelledError
            if outcome == "cancel"
            else type(failure)
        )
        with pytest.raises(error_type):
            await asyncio.wait_for(admission, timeout=2)
        replacement.list_tools.assert_not_awaited()
    failed_connection.list_tools.assert_awaited_once()
    if outcome == "permanent":
        recovery.assert_not_awaited()
        waiter.assert_not_awaited()
    else:
        recovery.assert_awaited_once_with(
            _runtime_session_factory,
            conversation_id="conv-inventory-recovery",
            task_id=None,
            executor_id=executor_id,
            notice_dispatcher=None,
        )
        waiter.assert_awaited_once()
    catalogs = [
        record for record in runtime_timing_records if record["stage"] == "runtime.catalog_read"
    ]
    attempts = [
        record for record in runtime_timing_records if record["stage"] == "runtime.build_attempt"
    ]
    waits = [
        record for record in runtime_timing_records if record["stage"] == "runtime.recovery_wait"
    ]
    assert catalogs[0]["outcome"] == attempts[0]["outcome"] == "interrupted"
    assert len(attempts) == len(catalogs) == (2 if outcome == "ready" else 1)
    assert len(waits) == (0 if outcome == "permanent" else 1)
    assert any(record["stage"] == "runtime.pin_read" for record in runtime_timing_records)
    if waits:
        assert waits[0]["outcome"] == ("interrupted" if outcome == "cancel" else "completed")
    if outcome == "ready":
        assert catalogs[-1]["outcome"] == attempts[-1]["outcome"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["conversation", "task"])
@pytest.mark.parametrize("mode", ["ready", "restart", "deadline"])
async def test_inventory_recovery_preserves_durable_deadline_until_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str, mode: str
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from cognis.core.executor_recovery import ExecutorRecoveryTimeout, begin_executor_recovery
    from cognis.providers.executor.delivery import DeliveryState, ExecutorDeliveryError

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/inventory-recovery.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    user_email = "inventory-owner@example.com"
    executor_id = "inventory-executor"
    conversation_id = "inventory-conversation"
    task_id = "inventory-task" if scope == "task" else None
    database_time = datetime.now(UTC)
    original_observation = database_time - timedelta(seconds=899 if mode == "deadline" else 60)
    original_deadline = original_observation + timedelta(seconds=900)
    try:
        async with sessions() as session:
            session.add(User(email=user_email, name="U", role="user"))
            await session.flush()
            session.add(Agent(agent_id="inventory-agent", owner_email=user_email, name="A"))
            await session.flush()
            pin = {
                "active_executor_id": executor_id,
                "active_executor_source": "explicit_primary",
                "active_executor_generation": 2,
                "active_executor_unavailable_since": original_observation,
            }
            session.add(
                Conversation(
                    conversation_id=conversation_id,
                    user_email=user_email,
                    agent_id="inventory-agent",
                    context_type="chat",
                    **pin,
                )
            )
            if task_id:
                session.add(
                    Task(
                        task_id=task_id,
                        title="Inventory recovery",
                        created_by=user_email,
                        agent_id="inventory-agent",
                        **pin,
                    )
                )
            await session.commit()

        async def database_now(_session):
            return database_time

        monkeypatch.setattr("cognis.store.coordination.database_now", database_now)
        monkeypatch.setattr(
            store_queries,
            "list_executors",
            AsyncMock(return_value=[_executor_row(executor_id, owner_email=user_email)]),
        )
        monkeypatch.setattr(
            runtime_support, "load_executor_policy", AsyncMock(return_value=ExecutorPolicy())
        )
        monkeypatch.setattr(
            runtime_support,
            "load_executor_pin_lifecycle_settings",
            _executor_pin_lifecycle_settings,
        )
        monkeypatch.setattr(
            runtime_support,
            "_resolve_web_config",
            AsyncMock(return_value={"web_available_backends": ["direct"], "web_backend": "direct"}),
        )
        monkeypatch.setattr(
            runtime_support, "resolve_skills_for_agent", AsyncMock(return_value=ResolvedSkillSet())
        )
        recovery = AsyncMock(wraps=begin_executor_recovery)
        monkeypatch.setattr(runtime_support, "begin_executor_recovery", recovery)
        ws = _ReadyWebSocketProvider(executor_id)
        ws.get_ready_connection = ws.get_connection
        attempts = 0

        async def list_tools():
            nonlocal attempts
            attempts += 1
            async with sessions() as session:
                conversation = await store_queries.get_conversation(session, conversation_id)
                assert (
                    conversation.active_executor_unavailable_since.replace(tzinfo=UTC)
                    == original_observation
                )
                if task_id:
                    task = await store_queries.get_task(session, task_id)
                    assert (
                        task.active_executor_unavailable_since.replace(tzinfo=UTC)
                        == original_observation
                    )
            if attempts <= 2:
                raise ExecutorDeliveryError(
                    "Short reconnect retry expired",
                    DeliveryState.TERMINAL,
                    code="executor_recovery_timeout",
                )
            return []

        ws.connection.list_tools = AsyncMock(side_effect=list_tools)
        waits = 0

        async def wait_for_connection(selected_id, **kwargs):
            nonlocal waits, database_time
            waits += 1
            assert selected_id == executor_id
            if mode == "restart" and waits == 1:
                raise asyncio.CancelledError
            if mode == "deadline":
                database_time += timedelta(seconds=2)
            return ws.connection

        ws.wait_for_connection = AsyncMock(side_effect=wait_for_connection)
        providers = SimpleNamespace(
            _session_factory=sessions,
            executor=SimpleNamespace(websocket=ws, status_provider=None),
        )

        def factory():
            return runtime_support.build_step_runtime_factory(
                providers=providers,
                shared_registry=ToolRegistry(),
                shared_connection=None,
                session_factory=sessions,
            )

        agent = AgentDefinition(
            agent_id="inventory-agent",
            name="A",
            owner_email=user_email,
            execution={"executor_id": executor_id},
        )
        kwargs = {"conversation_id": conversation_id, "task_id": task_id}
        if task_id:
            # Tasks release execution capacity and retry through durable admission.
            for _ in range(2):
                with pytest.raises(TransientExecutorUnavailable) as unavailable:
                    await factory()(agent, user_email, **kwargs)
                assert unavailable.value.executor_id == executor_id
                assert unavailable.value.retry_after_seconds == 30
                if mode == "deadline":
                    database_time += timedelta(seconds=2)
            assert waits == 0
            assert attempts == 2
            if mode != "deadline":
                runtime = await factory()(agent, user_email, **kwargs)
                assert runtime.executor_connection is ws.connection
                assert attempts == 3
        else:
            if mode == "restart":
                with pytest.raises(asyncio.CancelledError):
                    await factory()(agent, user_email, **kwargs)
            if mode == "deadline":
                with pytest.raises(ExecutorRecoveryTimeout) as error:
                    await factory()(agent, user_email, **kwargs)
                assert error.value.window.deadline == original_deadline
                assert error.value.phase == "runtime_admission"
                assert attempts == 2
            else:
                runtime = await factory()(agent, user_email, **kwargs)
                assert runtime.executor_connection is ws.connection
                assert attempts == 3
        assert recovery.await_count == 2
        async with sessions() as session:
            conversation = await store_queries.get_conversation(session, conversation_id)
            marker = conversation.active_executor_unavailable_since
            assert (marker.replace(tzinfo=UTC) if marker else None) == (
                original_observation if mode == "deadline" else None
            )
            if task_id:
                task = await store_queries.get_task(session, task_id)
                assert task.active_executor_unavailable_since == marker
    finally:
        await engine.dispose()


class _InProcessExecutorProvider:
    def __init__(self) -> None:
        self.websocket = _MissingWebSocketProvider()
        self.status_provider = None
        self.spawned_ids: list[str] = []
        self.cancelled_ids: list[str] = []
        self.registry = ToolRegistry()
        self.spawned_configs: list[object] = []

    async def spawn(self, config: object) -> ExecutorHandle:
        executor_id = config.executor_id  # type: ignore[attr-defined]
        self.spawned_configs.append(config)
        self.spawned_ids.append(executor_id)
        return ExecutorHandle(executor_id=executor_id, executor_type="in_process")

    async def get_executor(self, handle: ExecutorHandle) -> SimpleNamespace:
        return SimpleNamespace(registry=self.registry, handle=handle)

    async def cancel(self, handle: ExecutorHandle) -> None:
        self.cancelled_ids.append(handle.executor_id)


class _RuntimeSessionFactory:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _runtime_session_factory() -> _RuntimeSessionFactory:
    return _RuntimeSessionFactory()


class _WebSecretsProvider:
    async def get_secret(self, name: str, _user_email: str) -> str:
        return {
            "tavily_api_key": "tavily-key",
            "brave_api_key": "brave-key",
        }[name]


@pytest.mark.asyncio
async def test_resolve_web_config_excludes_disabled_backends_and_normalizes_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, object] = {
        "web.backend": "tavily",
        "web.search_backend": "brave",
        "web.fetch_backend": "tavily",
        "web.tavily_enabled": False,
        "web.brave_enabled": False,
        "web.searxng_enabled": False,
        "web.searxng_url": "https://search.example.com",
    }

    async def _get_setting_value(_session: object, key: str, default: object = None) -> object:
        return values.get(key, default)

    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    providers = SimpleNamespace(
        _session_factory=_runtime_session_factory,
        secrets=_WebSecretsProvider(),
    )

    config = await runtime_support._resolve_web_config(providers, "user@example.com")

    assert config["web_search_backend"] == "direct"
    assert config["web_fetch_backend"] == "direct"
    assert config["web_backend"] == "direct"
    assert config["web_available_search_backends"] == ["direct"]
    assert config["web_available_fetch_backends"] == ["direct", "browser"]
    assert config["web_searxng_url"] == ""
    assert config["web_secrets"] == {}


@pytest.mark.asyncio
async def test_resolve_web_config_keeps_configured_backends_enabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, object] = {
        "web.search_backend": "searxng",
        "web.fetch_backend": "tavily",
        "web.searxng_url": "https://search.example.com",
    }

    async def _get_setting_value(_session: object, key: str, default: object = None) -> object:
        return values.get(key, default)

    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    providers = SimpleNamespace(
        _session_factory=_runtime_session_factory,
        secrets=_WebSecretsProvider(),
    )

    config = await runtime_support._resolve_web_config(providers, "user@example.com")

    assert config["web_search_backend"] == "searxng"
    assert config["web_fetch_backend"] == "tavily"
    assert config["web_available_search_backends"] == [
        "direct",
        "tavily",
        "brave",
        "searxng",
    ]
    assert config["web_available_fetch_backends"] == ["direct", "tavily", "browser"]


@pytest.mark.asyncio
async def test_resolve_web_config_fails_closed_when_settings_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_setting_value(_session: object, _key: str, _default: object = None) -> object:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store_queries, "get_setting_value", _get_setting_value)
    providers = SimpleNamespace(
        _session_factory=_runtime_session_factory,
        secrets=_WebSecretsProvider(),
    )

    config = await runtime_support._resolve_web_config(providers, "user@example.com")

    assert config["web_backend"] == "direct"
    assert config["web_search_backend"] == "direct"
    assert config["web_fetch_backend"] == "direct"
    assert config["web_available_search_backends"] == ["direct"]
    assert config["web_available_fetch_backends"] == ["direct", "browser"]
    assert config["web_secrets"] == {}


def _shared_in_process_connection() -> InProcessExecutorConnection:
    return InProcessExecutorConnection(
        ExecutorHandle(executor_id="controller_shared_builtin", executor_type="in_process"),
        ToolRegistry(),
        breaker=object(),  # type: ignore[arg-type]
    )


def _runtime_providers() -> SimpleNamespace:
    return SimpleNamespace(
        _session_factory=_runtime_session_factory,
        executor=SimpleNamespace(websocket=_MissingWebSocketProvider(), status_provider=None),
    )


def _runtime_providers_with_ws(ws_provider: object) -> SimpleNamespace:
    return SimpleNamespace(
        _session_factory=_runtime_session_factory,
        executor=SimpleNamespace(websocket=ws_provider, status_provider=None),
    )


def _runtime_providers_with_executor(executor_provider: object) -> SimpleNamespace:
    return SimpleNamespace(
        _session_factory=_runtime_session_factory,
        executor=executor_provider,
        secrets=_SecretsProvider(),
    )


def _executor_row(
    executor_id: str,
    *,
    owner_email: str | None = "alice@example.com",
    executor_type: str = "websocket",
    labels: dict[str, object] | None = None,
    status: str = "active",
    is_default: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        name=executor_id,
        executor_type=executor_type,
        labels=labels or {},
        enabled_tools=[],
        enabled_tool_groups=[],
        config={},
        status=status,
        is_default=is_default,
        owner_email=owner_email,
        desired_config_version=1,
        applied_config_version=1,
        observed_tools=[],
        last_observed_at=None,
        runtime_state="active",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["pin", "selector", "concurrent_winner"])
async def test_runtime_build_reuses_selection_pool_but_refreshes_admission(monkeypatch, selection):
    from unittest.mock import AsyncMock

    selected = _executor_row("selected", labels={"role": "worker"})
    winner = _executor_row("winner", labels={"role": "worker"})
    rows = AsyncMock(return_value=[selected])
    monkeypatch.setattr(store_queries, "list_executors", rows)
    monkeypatch.setattr(store_queries, "get_executor_row", AsyncMock(return_value=selected))
    monkeypatch.setattr(
        runtime_support, "load_executor_policy", AsyncMock(return_value=ExecutorPolicy())
    )
    monkeypatch.setattr(
        runtime_support, "resolve_skills_for_agent", AsyncMock(return_value=ResolvedSkillSet())
    )
    execution = {
        "executor_selector": {"role": "worker"},
        "additional_executors": [{"executor_id": "selected"}],
    }
    if selection == "pin":
        execution["executor_id"] = "selected"
    monkeypatch.setattr(
        store_queries,
        "get_conversation",
        AsyncMock(
            return_value=SimpleNamespace(
                active_executor_id="selected" if selection == "pin" else None
            )
        ),
    )
    monkeypatch.setattr(
        runtime_support,
        "_resolve_executor_scope",
        AsyncMock(return_value=(execution, "alice@example.com", False)),
    )
    if selection == "concurrent_winner":
        rows.side_effect = [[selected], [selected, winner]]
        monkeypatch.setattr(
            runtime_support,
            "_resolve_eligible_executor_config",
            AsyncMock(
                return_value={
                    "executor_id": "winner",
                    "executor_type": "websocket",
                    "config": {},
                    "enabled_tools": [],
                    "enabled_tool_groups": [],
                }
            ),
        )
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers(),
        shared_registry=ToolRegistry(),
        shared_connection=None,
        session_factory=_runtime_session_factory,
    )
    runtime = await factory(
        agent=AgentDefinition(
            agent_id="pool-agent", name="Pool", owner_email="alice@example.com", execution=execution
        ),
        user_email="alice@example.com",
        prepare_only=True,
        conversation_id="pool-conversation",
    )
    assert rows.await_count == (2 if selection == "concurrent_winner" else 1)
    assert (
        runtime.executor_pool.by_id("winner" if selection == "concurrent_winner" else "selected")
        is not None
    )
    assert runtime.executor_pool.additional == []
    rows.side_effect = None
    rows.return_value = []
    refreshed = await runtime.refresh_executor_pool(runtime.executor_pool)
    assert not refreshed.by_id("selected").usable
    assert rows.call_args.kwargs["defer_observed_tools"] is True


@pytest.mark.asyncio
async def test_runtime_factory_refuses_missing_explicit_executor_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers(),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    from cognis.core.runtime import ExecutorConfigurationError
    from cognis.core.turn_scheduler import (
        _durable_turn_error_detail,
        _durable_turn_error_message,
        classify_turn_error,
    )

    providers = _runtime_providers()
    with pytest.raises(
        ExecutorConfigurationError, match="explicitly configure executor_id or executor_selector"
    ) as failure:
        await factory(
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="alice@example.com",
                name="Agent",
            ),
            user_email="alice@example.com",
        )

    classified = await classify_turn_error(providers, failure.value)
    assert classified.code == "executor_configuration_invalid"
    assert classified.recoverable is False
    assert classified.transient is False
    assert "agent page" in classified.message
    assert "agent page" in _durable_turn_error_message(classified)
    assert _durable_turn_error_detail(classified) == str(failure.value)


@pytest.mark.asyncio
async def test_runtime_factory_refuses_empty_executor_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers(),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    from cognis.core.runtime import ExecutorConfigurationError

    with pytest.raises(
        ExecutorConfigurationError, match="executor_selector must be a non-empty object"
    ):
        await factory(
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="alice@example.com",
                name="Agent",
                execution={"executor_selector": {}},
            ),
            user_email="alice@example.com",
        )


@pytest.mark.asyncio
async def test_eligible_executor_allows_explicit_shared_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return _executor_row("shared_exec", owner_email=None)

    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "shared_exec", "executor_selector": {}},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
    )

    assert config["executor_id"] == "shared_exec"
    assert config["selection_source"] == "explicit"


@pytest.mark.asyncio
async def test_eligible_executor_selector_picks_one_when_multi_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 36: a primary selector matching N>=1 usable executors no longer raises.

    The controller picks one usable primary and persists it.
    """

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [
            _executor_row("exec-a", labels={"role": "local"}),
            _executor_row("exec-b", labels={"role": "local"}),
        ]

    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace | None:
        return None

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_selector": {"role": "local"}},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
    )
    # Stage 36 picks the lexicographically smallest usable primary at ties
    assert config["executor_id"] == "exec-a"
    assert config["selection_source"] == "selector"


@pytest.mark.asyncio
async def test_eligible_executor_selector_can_pick_shared_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [
            _executor_row("shared_exec", owner_email=None, labels={"pool": "shared"}),
        ]

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_selector": {"pool": "shared"}},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
    )

    assert config["executor_id"] == "shared_exec"
    assert config["selection_source"] == "selector"


@pytest.mark.asyncio
async def test_eligible_executor_uses_grantee_override_for_grantee_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_grant(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            executor_scope="grantee_executor",
            grantee_overrides={"execution": {"executor_id": "guest_exec"}},
        )

    async def _get_executor_row(*_: object, **kwargs: object) -> SimpleNamespace | None:
        assert kwargs["owner_email"] == "guest@example.com"
        return _executor_row("guest_exec", owner_email="guest@example.com")

    monkeypatch.setattr(store_queries, "get_active_agent_grant", _get_grant)
    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="owner@example.com",
            name="Agent",
            execution={"executor_id": "owner_exec"},
        ),
        "guest@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
    )

    assert config["executor_id"] == "guest_exec"
    assert config["executor_owner_email"] == "guest@example.com"


@pytest.mark.asyncio
async def test_eligible_executor_falls_back_to_grantee_default_for_grantee_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_grant(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(executor_scope="grantee_executor", grantee_overrides={})

    async def _list_executors(*_: object, **kwargs: object) -> list[SimpleNamespace]:
        assert kwargs["owner_email"] == "guest@example.com"
        return [_executor_row("guest_default", owner_email="guest@example.com", is_default=True)]

    monkeypatch.setattr(store_queries, "get_active_agent_grant", _get_grant)
    monkeypatch.setattr(store_queries, "list_executors", _list_executors)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="owner@example.com",
            name="Agent",
            execution={},
        ),
        "guest@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
    )

    assert config["executor_id"] == "guest_default"
    assert config["selection_source"] == "default"


@pytest.mark.asyncio
async def test_grantee_without_default_gets_actionable_executor_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.core.runtime import ExecutorConfigurationError
    from cognis.core.turn_scheduler import classify_turn_error

    async def grant(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(executor_scope="grantee_executor", grantee_overrides={})

    async def executors(*_: object, **__: object) -> list[object]:
        return []

    monkeypatch.setattr(store_queries, "get_active_agent_grant", grant)
    monkeypatch.setattr(store_queries, "list_executors", executors)

    async def policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy()

    monkeypatch.setattr(runtime_support, "load_executor_policy", policy)
    providers = _runtime_providers()
    factory = runtime_support.build_step_runtime_factory(
        providers=providers,
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )
    with pytest.raises(ExecutorConfigurationError) as failure:
        await factory(
            agent=AgentDefinition(
                agent_id="shared-reader", owner_email="owner@example.com", name="Shared reader"
            ),
            user_email="guest@example.com",
            prepare_only=True,
        )
    error = await classify_turn_error(providers, failure.value)
    assert error.code == "executor_configuration_invalid"
    assert "your account" in error.detail["client_safe_detail"]
    assert "Settings > Executors" in error.detail["client_safe_detail"]


@pytest.mark.asyncio
async def test_runtime_factory_refuses_explicit_websocket_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _eligible_executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": "alice_exec",
            "executor_type": "websocket",
            "enabled_tools": [],
            "enabled_tool_groups": [],
            "config": {},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _eligible_executor_config
    )
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers(),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    with pytest.raises(RuntimeError, match="not connected or not ready"):
        await factory(
            agent=AgentDefinition(
                agent_id="agent-1",
                owner_email="alice@example.com",
                name="Agent",
                execution={"executor_id": "alice_exec"},
            ),
            user_email="alice@example.com",
        )


@pytest.mark.asyncio
async def test_runtime_factory_returns_runtime_diagnostics_for_selected_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _eligible_executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": "alice_exec",
            "executor_type": "websocket",
            "enabled_tools": [],
            "enabled_tool_groups": [],
            "config": {},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "selection_source": "explicit",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _eligible_executor_config
    )
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    providers = _runtime_providers_with_ws(_ReadyWebSocketProvider("alice_exec"))
    providers.image_generation = SimpleNamespace(
        resolve_model_target=AsyncMock(return_value=("gpt-image-1", "openai")),
        list_models=AsyncMock(return_value=[{"provider_id": "openai", "model_id": "gpt-image-1"}]),
    )
    factory = runtime_support.build_step_runtime_factory(
        providers=providers,
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    runtime = await factory(
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "alice_exec"},
        ),
        user_email="alice@example.com",
    )

    assert runtime.runtime_info is not None
    assert runtime.runtime_info["runtime_source"] == "remote_executor"
    assert runtime.runtime_info["fallback_used"] is False
    assert runtime.runtime_info["executor_id"] == "alice_exec"
    assert runtime.runtime_info["environment"]["cwd"] == "/workspace"

    first_image = runtime.tool_registry.get("image_generate").definition
    assert first_image.parameters["properties"]["model"]["enum"] == ["openai/gpt-image-1"]
    providers.image_generation.resolve_model_target.return_value = (
        "gemini-nano-banana-2.1",
        "gemini",
    )
    providers.image_generation.list_models.return_value = [
        {
            "provider_id": "gemini",
            "model_id": "gemini-nano-banana-2.1",
            "supports_image_generation": True,
        }
    ]
    refreshed = await factory(
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "alice_exec"},
        ),
        user_email="alice@example.com",
    )
    refreshed_image = refreshed.tool_registry.get("image_generate").definition
    assert refreshed_image.parameters["properties"]["model"]["enum"] == [
        "gemini/gemini-nano-banana-2.1"
    ]
    assert (
        "Default: gemini/gemini-nano-banana-2.1."
        in (refreshed_image.parameters["properties"]["model"]["description"])
    )
    providers.image_generation.list_models.assert_awaited_with(
        acting_user_email="alice@example.com"
    )
    assert first_image.parameters["properties"]["model"]["enum"] == ["openai/gpt-image-1"]


@pytest.mark.asyncio
async def test_runtime_factory_materializes_auto_loaded_skill_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _eligible_executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": "alice_exec",
            "executor_type": "websocket",
            "enabled_tools": [],
            "enabled_tool_groups": [],
            "config": {},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "selection_source": "explicit",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet(
            skills=[
                ResolvedSkill(
                    skill_id="cognis-coding",
                    name="Cognis Coding",
                    description="Coding discipline",
                    linked_tool_ids=["builtin:bash"],
                    version_id="sv_1",
                    version_number=1,
                    content_hash="hash",
                    instructions="Use careful implementation discipline.",
                    tools=[SkillToolSpec(name="run_check", description="Run checks")],
                    attached=True,
                    auto_load_instructions=True,
                )
            ]
        )

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _eligible_executor_config
    )
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    agent = AgentDefinition(
        agent_id="agent-1",
        owner_email="alice@example.com",
        name="Agent",
        execution={"executor_id": "alice_exec"},
    )
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers_with_ws(_ReadyWebSocketProvider("alice_exec")),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    runtime = await factory(agent=agent, user_email="alice@example.com")

    assert runtime.tool_registry.get("skill_cognis-coding__run_check") is not None
    assert isinstance(agent.skills, dict)
    assert agent.skills["_auto_loaded_skill_ids"] == ["cognis-coding"]
    assert (
        "Use careful implementation discipline." in agent.skills["_auto_loaded_skill_contexts"][0]
    )
    assert agent.skills["_auto_loaded_skill_tool_ids"] == [
        "builtin:bash",
        "skill:cognis-coding:run_check",
    ]


@pytest.mark.asyncio
async def test_runtime_factory_uses_unique_in_process_handle_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(_: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def _eligible_executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": "alice_local",
            "executor_type": "in_process",
            "enabled_tools": [],
            "enabled_tool_groups": [],
            "config": {},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "selection_source": "explicit",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": ["direct"], "web_backend": "direct"}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _eligible_executor_config
    )
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    executor = _InProcessExecutorProvider()
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers_with_executor(executor),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    first = await factory(
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "alice_local"},
        ),
        user_email="alice@example.com",
    )
    second = await factory(
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "alice_local"},
        ),
        user_email="alice@example.com",
    )

    try:
        assert len(executor.spawned_ids) == 2
        assert executor.spawned_ids[0] != executor.spawned_ids[1]
        assert all(spawned.startswith("alice_local:run:") for spawned in executor.spawned_ids)
        assert first.runtime_info is not None
        assert first.runtime_info["executor_id"] == "alice_local"
        assert second.runtime_info is not None
        assert second.runtime_info["executor_id"] == "alice_local"
    finally:
        await first.cleanup()
        await second.cleanup()

    assert executor.cancelled_ids == executor.spawned_ids


@pytest.mark.asyncio
async def test_direct_in_process_runtime_receives_knowledgebase_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(*_: object, **__: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=False)

    async def _eligible_executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": "alice_local",
            "executor_type": "in_process",
            "enabled_tools": [],
            "enabled_tool_groups": [],
            "config": {},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "selection_source": "explicit",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {"web_available_backends": [], "web_backend": None}

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    class _KnowledgebaseService:
        enabled = True

        async def list(
            self, *, owner_email: str, access_context: object | None = None
        ) -> list[KnowledgebaseModel]:
            return [KnowledgebaseModel(knowledgebase_id="kb-1", name=owner_email)]

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _eligible_executor_config
    )
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    executor = _InProcessExecutorProvider()
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers_with_executor(executor),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
        knowledgebase_service=_KnowledgebaseService(),
    )

    runtime = await factory(
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "alice_local"},
        ),
        user_email="alice@example.com",
    )

    try:
        config = executor.spawned_configs[0]
        assert "knowledgebase_list" in {
            tool.name
            for tool in config.tools  # type: ignore[attr-defined]
        }
        handler = config.tool_handlers["knowledgebase_list"]  # type: ignore[attr-defined]
        result = await handler(
            {},
            ToolExecutionContext(
                executor_handle=runtime.executor_connection.handle,
                runtime_metadata={"user_email": "alice@example.com"},
            ),
        )
        assert result[0]["knowledgebase_id"] == "kb-1"
        assert result[0]["name"] == "alice@example.com"
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
async def test_runtime_factory_applies_agent_deny_to_dynamic_web_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _policy(*_: object, **__: object) -> ExecutorPolicy:
        return ExecutorPolicy(allow_in_process=True, allow_subprocess=False)

    async def _eligible_executor_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "executor_id": "alice_local",
            "executor_type": "in_process",
            "enabled_tools": ["*"],
            "enabled_tool_groups": ["web"],
            "config": {},
            "executor_owner_email": "alice@example.com",
            "owner_email": "alice@example.com",
            "selection_source": "explicit",
            "runtime_state": "active",
            "desired_config_version": 1,
            "applied_config_version": 1,
        }

    async def _web_config(*_: object, **__: object) -> dict[str, object]:
        return {
            "web_available_backends": ["direct"],
            "web_backend": "direct",
            "web_available_search_backends": ["direct"],
            "web_available_fetch_backends": ["direct"],
            "web_search_backend": "direct",
            "web_fetch_backend": "direct",
        }

    async def _skills(*_: object, **__: object) -> ResolvedSkillSet:
        return ResolvedSkillSet()

    monkeypatch.setattr(runtime_support, "load_executor_policy", _policy)
    monkeypatch.setattr(
        runtime_support, "_resolve_eligible_executor_config", _eligible_executor_config
    )
    monkeypatch.setattr(runtime_support, "_resolve_web_config", _web_config)
    monkeypatch.setattr(runtime_support, "resolve_skills_for_agent", _skills)

    executor = _InProcessExecutorProvider()
    factory = runtime_support.build_step_runtime_factory(
        providers=_runtime_providers_with_executor(executor),
        shared_registry=ToolRegistry(),
        shared_connection=_shared_in_process_connection(),
        session_factory=_runtime_session_factory,
    )

    runtime = await factory(
        agent=AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "alice_local"},
            tools={"deny_tools": ["builtin:web_search"]},
        ),
        user_email="alice@example.com",
    )

    try:
        config = executor.spawned_configs[0]
        tool_names = {tool.name for tool in config.tools}  # type: ignore[attr-defined]
        assert "web_crawl" in tool_names
        assert "web_search" not in tool_names
    finally:
        await runtime.cleanup()


# ---------------------------------------------------------------------------
# Stage 36: initial active executor pick + conversation-level pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_active_executor_persisted_for_explicit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 36: explicit executor_id is persisted as the conversation's initial active."""

    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return _executor_row("exec-1")

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [_executor_row("exec-1")]

    persisted: dict[str, str | None] = {"id": None, "source": None}

    async def _initialize(_session, conversation_id, executor_id, *, source):
        persisted["id"] = executor_id
        persisted["source"] = source
        return True

    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(
        store_queries,
        "initialize_conversation_active_executor",
        _initialize,
    )

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={"executor_id": "exec-1"},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_id="conv-1",
    )
    assert config["executor_id"] == "exec-1"
    assert persisted["id"] == "exec-1"
    assert persisted["source"] == "explicit_primary"


@pytest.mark.asyncio
async def test_initial_active_executor_persisted_for_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_executor_row(*_: object, **__: object) -> SimpleNamespace:
        return _executor_row("exec-selector")

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [_executor_row("exec-selector", labels={"role": "local"})]

    persisted: dict[str, str | None] = {"id": None, "source": None}

    async def _initialize(_session, conversation_id, executor_id, *, source):
        persisted["id"] = executor_id
        persisted["source"] = source
        return True

    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)
    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(store_queries, "initialize_conversation_active_executor", _initialize)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-selector",
            owner_email="alice@example.com",
            name="Selector",
            execution={"executor_selector": {"role": "local"}},
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_id="conv-selector",
    )
    assert config["executor_id"] == "exec-selector"
    assert persisted == {"id": "exec-selector", "source": "selector_primary"}


@pytest.mark.asyncio
async def test_pinned_active_executor_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 36: conversation_active_executor_id pins runtime to that executor."""

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [
            _executor_row("exec-pri", labels={"tier": "primary"}),
            _executor_row("exec-add"),
        ]

    async def _get_executor_row(_session, executor_id, **_kwargs):
        if executor_id == "exec-pri":
            return _executor_row("exec-pri", labels={"tier": "primary"})
        if executor_id == "exec-add":
            return _executor_row("exec-add")
        return None

    monkeypatch.setattr(
        runtime_support,
        "load_executor_pin_lifecycle_settings",
        _executor_pin_lifecycle_settings,
    )
    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)

    config = await runtime_support._resolve_eligible_executor_config(
        _runtime_providers(),
        AgentDefinition(
            agent_id="agent-1",
            owner_email="alice@example.com",
            name="Agent",
            execution={
                "executor_id": "exec-pri",
                "additional_executors": [{"executor_id": "exec-add"}],
            },
        ),
        "alice@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_active_executor_id="exec-add",
        conversation_active_executor_source="additional",
    )
    # Pin overrides the primary binding
    assert config["executor_id"] == "exec-add"
    assert config["selection_source"] == "conversation_active_additional"


@pytest.mark.asyncio
async def test_pinned_unassigned_executor_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 36: pinned executor that's no longer assigned raises a factual error."""

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [_executor_row("exec-pri")]

    async def _get_executor_row(_session, executor_id, **_kwargs):
        if executor_id == "exec-pri":
            return _executor_row("exec-pri")
        return None

    monkeypatch.setattr(
        runtime_support,
        "load_executor_pin_lifecycle_settings",
        _executor_pin_lifecycle_settings,
    )
    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(store_queries, "get_executor_row", _get_executor_row)

    with pytest.raises(RuntimeError, match="no longer assigned"):
        await runtime_support._resolve_eligible_executor_config(
            _runtime_providers(),
            AgentDefinition(
                agent_id="agent-1",
                owner_email="alice@example.com",
                name="Agent",
                execution={"executor_id": "exec-pri"},
            ),
            "alice@example.com",
            ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
            conversation_active_executor_id="exec-ghost",
            conversation_active_executor_source="explicit_primary",
        )


@pytest.mark.asyncio
async def test_missing_selector_pin_in_grace_is_transient_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [_executor_row("replacement", labels={"role": "local"})]

    async def _lifecycle(**kwargs: object) -> ExecutorPinLifecycleResult:
        assert kwargs["active_executor_source"] == "selector_primary"
        return ExecutorPinLifecycleResult(
            active_executor_id="missing",
            transient_unavailable=True,
            retry_after_seconds=7,
        )

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    monkeypatch.setattr(
        runtime_support,
        "load_executor_pin_lifecycle_settings",
        _executor_pin_lifecycle_settings,
    )
    monkeypatch.setattr(runtime_support, "ensure_active_executor_pin", _lifecycle)

    with pytest.raises(TransientExecutorUnavailable) as error:
        await runtime_support._resolve_eligible_executor_config(
            _runtime_providers(),
            AgentDefinition(
                agent_id="agent-selector",
                owner_email="alice@example.com",
                name="Selector",
                execution={"executor_selector": {"role": "local"}},
            ),
            "alice@example.com",
            ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
            conversation_id="conv-selector",
            conversation_active_executor_id="missing",
            conversation_active_executor_source="selector_primary",
            conversation_active_executor_generation=3,
        )
    assert error.value.executor_id == "missing"
    assert error.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_legacy_null_selector_source_resolves_and_canonicalizes_on_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/runtime-pin-source.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="runtime-source@example.com", name="U", role="user"))
        await session.flush()
        session.add(
            Agent(
                agent_id="runtime-source-agent",
                owner_email="runtime-source@example.com",
                name="A",
            )
        )
        await session.flush()
        session.add(
            Conversation(
                conversation_id="runtime-source-conv",
                user_email="runtime-source@example.com",
                agent_id="runtime-source-agent",
                context_type="chat",
                active_executor_id="ready-selector",
                active_executor_source=None,
                active_executor_generation=2,
            )
        )
        session.add(
            Task(
                task_id="runtime-source-task",
                title="T",
                created_by="runtime-source@example.com",
                agent_id="runtime-source-agent",
                active_executor_id="ready-selector",
                active_executor_source=None,
                active_executor_generation=2,
            )
        )
        await session.commit()

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return [
            _executor_row(
                "ready-selector",
                owner_email="runtime-source@example.com",
                labels={"role": "local"},
            )
        ]

    class _ReadyWebSocket:
        def get_ready_connection(self, executor_id: str) -> SimpleNamespace | None:
            return self.get_connection(executor_id)

        def get_connection(self, executor_id: str) -> SimpleNamespace | None:
            return SimpleNamespace(connected=True) if executor_id == "ready-selector" else None

        def get_handle(self, executor_id: str) -> SimpleNamespace | None:
            return SimpleNamespace(status="ready") if executor_id == "ready-selector" else None

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    providers = SimpleNamespace(
        _session_factory=factory,
        executor=SimpleNamespace(websocket=_ReadyWebSocket(), status_provider=None),
    )
    config = await runtime_support._resolve_eligible_executor_config(
        providers,
        AgentDefinition(
            agent_id="runtime-source-agent",
            owner_email="runtime-source@example.com",
            name="A",
            execution={"executor_selector": {"role": "local"}},
        ),
        "runtime-source@example.com",
        ExecutorPolicy(allow_in_process=True, allow_subprocess=True),
        conversation_id="runtime-source-conv",
        task_id="runtime-source-task",
        conversation_active_executor_id="ready-selector",
        conversation_active_executor_source=None,
        conversation_active_executor_generation=2,
    )
    assert config["executor_id"] == "ready-selector"

    async with factory() as session:
        task = await store_queries.get_task(session, "runtime-source-task")
        conversation = await store_queries.get_conversation(session, "runtime-source-conv")
        assert task is not None and conversation is not None
        assert task.active_executor_source == "selector_primary"
        assert conversation.active_executor_source == "selector_primary"
        assert task.active_executor_generation == conversation.active_executor_generation == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_missing_selector_reconnect_preserves_pin_on_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/runtime-missing-selector.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    user_email = "runtime-missing@example.com"
    async with factory() as session:
        session.add(User(email=user_email, name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="runtime-missing-agent", owner_email=user_email, name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="runtime-missing-conv",
                user_email=user_email,
                agent_id="runtime-missing-agent",
                context_type="chat",
                active_executor_id="missing-selector",
                active_executor_generation=2,
            )
        )
        session.add(
            Task(
                task_id="runtime-missing-task",
                title="T",
                created_by=user_email,
                agent_id="runtime-missing-agent",
                active_executor_id="missing-selector",
                active_executor_generation=2,
            )
        )
        await session.commit()

    rows = [_executor_row("replacement", owner_email=user_email, labels={"role": "local"})]
    ready_ids = {"replacement"}

    async def _list_executors(*_: object, **__: object) -> list[SimpleNamespace]:
        return list(rows)

    class _ReadyWebSocket:
        def get_ready_connection(self, executor_id: str) -> SimpleNamespace | None:
            return self.get_connection(executor_id)

        def get_connection(self, executor_id: str) -> SimpleNamespace | None:
            return SimpleNamespace(connected=True) if executor_id in ready_ids else None

        def get_handle(self, executor_id: str) -> SimpleNamespace | None:
            return SimpleNamespace(status="ready") if executor_id in ready_ids else None

    monkeypatch.setattr(store_queries, "list_executors", _list_executors)
    providers = SimpleNamespace(
        _session_factory=factory,
        executor=SimpleNamespace(websocket=_ReadyWebSocket(), status_provider=None),
    )
    agent = AgentDefinition(
        agent_id="runtime-missing-agent",
        owner_email=user_email,
        name="A",
        execution={"executor_selector": {"role": "local"}},
    )
    policy = ExecutorPolicy(allow_in_process=True, allow_subprocess=True)

    async def resolve(
        *,
        source: str | None,
        unavailable_since: object = None,
    ) -> dict[str, object]:
        return await runtime_support._resolve_eligible_executor_config(
            providers,
            agent,
            user_email,
            policy,
            conversation_id="runtime-missing-conv",
            task_id="runtime-missing-task",
            conversation_active_executor_id="missing-selector",
            conversation_active_executor_source=source,
            conversation_active_executor_generation=2,
            conversation_active_executor_unavailable_since=unavailable_since,
        )

    with pytest.raises(TransientExecutorUnavailable):
        await resolve(source=None)
    from cognis.core.executor_recovery import begin_executor_recovery

    await begin_executor_recovery(
        factory,
        conversation_id="runtime-missing-conv",
        task_id="runtime-missing-task",
        executor_id="missing-selector",
    )
    async with factory() as session:
        task = await store_queries.get_task(session, "runtime-missing-task")
        conversation = await store_queries.get_conversation(session, "runtime-missing-conv")
        assert task is not None and conversation is not None
        observed_at = task.active_executor_unavailable_since
        assert observed_at is not None
        assert (
            task.active_executor_source == conversation.active_executor_source == "selector_primary"
        )

    rows.insert(
        0,
        _executor_row("missing-selector", owner_email=user_email, labels={"role": "local"}),
    )
    ready_ids.add("missing-selector")
    recovered = await resolve(source="selector_primary", unavailable_since=observed_at)
    assert recovered["executor_id"] == "missing-selector"
    async with factory() as session:
        task = await store_queries.get_task(session, "runtime-missing-task")
        conversation = await store_queries.get_conversation(session, "runtime-missing-conv")
        assert task is not None and conversation is not None
        # Resolving the pin is not successful inventory admission.
        assert task.active_executor_unavailable_since == observed_at
        assert conversation.active_executor_unavailable_since == observed_at
        old_observation = datetime.now(UTC) - timedelta(seconds=30)
        task.active_executor_unavailable_since = old_observation
        conversation.active_executor_unavailable_since = old_observation
        await session.commit()

    rows.pop(0)
    ready_ids.remove("missing-selector")
    with pytest.raises(TransientExecutorUnavailable):
        await resolve(
            source="selector_primary",
            unavailable_since=old_observation,
        )
    async with factory() as session:
        task = await store_queries.get_task(session, "runtime-missing-task")
        conversation = await store_queries.get_conversation(session, "runtime-missing-conv")
        transition_count = await session.scalar(
            select(func.count()).select_from(ExecutorPinTransitionRow)
        )
        outbox_count = await session.scalar(
            select(func.count()).select_from(ExecutorPinNoticeOutboxRow)
        )
        assert task is not None and conversation is not None
        assert task.active_executor_id == conversation.active_executor_id == "missing-selector"
        assert task.active_executor_generation == conversation.active_executor_generation == 2
        assert transition_count == outbox_count == 0
    await engine.dispose()
