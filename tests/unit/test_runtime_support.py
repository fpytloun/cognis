from __future__ import annotations

from types import SimpleNamespace

import pytest

from cognis.api.runtime_support import (
    _build_remote_runtime_registry,
    _merge_remote_runtime_inventory,
    _resolve_intaris_mcp_tools,
)
from cognis.models.agent import AgentDefinition
from cognis.models.tool import ToolDefinition, ToolSource, sanitize_mcp_tool_name


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
            {
                "name": colliding_name,
                "description": "Remote tool",
                "parameters": {"type": "object", "properties": {}},
                "source": ToolSource(type="executor").model_dump(mode="json"),
                "category": "mcp",
                "read_only": True,
                "timeout_seconds": 30,
                "non_bypassable": False,
            }
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
