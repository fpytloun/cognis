from __future__ import annotations

from cognis.core.tool_exposure import prepare_tool_exposure
from cognis.models.config import ModelInfo
from cognis.models.tool import ToolDefinition, ToolSource, sanitize_mcp_tool_name, stable_tool_id
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL


def _tool(name: str, *, source_type: str = "builtin", category: str = "system") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type=source_type),
        category=category,
        read_only=True,
    )


def test_prepare_tool_exposure_uses_anthropic_deferred_loading() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        ToolDefinition(
            name=sanitize_mcp_tool_name("github", "search/issues"),
            description="search",
            parameters={"type": "object", "properties": {}},
            source=ToolSource(
                type="intaris_mcp", server_name="github", raw_tool_name="search/issues"
            ),
            category="mcp",
        ),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model="claude-sonnet-4-20250514",
        model_info=ModelInfo(model_id="claude-sonnet-4", supports_defer_loading=True),
        discovered_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "anthropic_defer_loading"
    assert result.request_kwargs["extra_headers"]["anthropic-beta"] == "tool-search-tool-2025-10-19"
    assert result.tools[-1]["function"]["cache_control"] == {"type": "ephemeral"}
    deferred = [tool for tool in result.tools if tool["function"]["name"].startswith("mcp_")]
    assert deferred[0]["function"]["defer_loading"] is True


def test_prepare_tool_exposure_uses_generic_search_fallback_with_discovered_tools() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("github", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
    )
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        mcp_tool,
    ]
    controller_search_schema = {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_TOOL.name,
            "description": SEARCH_TOOLS_TOOL.description,
            "parameters": SEARCH_TOOLS_TOOL.parameters,
        },
    }

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[controller_search_schema],
        model="gpt-4o-mini",
        model_info=ModelInfo(model_id="gpt-4o-mini", max_tools=3),
        discovered_tool_ids={stable_tool_id(mcp_tool)},
    )

    tool_names = [tool["function"]["name"] for tool in result.tools]
    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert "search_tools" in tool_names
    assert sanitize_mcp_tool_name("github", "search/issues") in tool_names


def test_prepare_tool_exposure_dedupes_visible_names() -> None:
    tool_a = ToolDefinition(
        name="same_name",
        description="a",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="local_mcp", server_name="srv-a", raw_tool_name="same/name"),
        category="mcp",
    )
    tool_b = ToolDefinition(
        name="same_name",
        description="b",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="srv-b", raw_tool_name="same.name"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[tool_a, tool_b],
        controller_tool_schemas=[],
        model="claude-sonnet-4-20250514",
        model_info=ModelInfo(model_id="claude", supports_defer_loading=True),
        discovered_tool_ids=set(),
    )

    visible_names = [tool["function"]["name"] for tool in result.tools]
    assert len(visible_names) == len(set(visible_names))


def test_prepare_tool_exposure_uses_openai_responses_full_inventory() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("github", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
    )
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        mcp_tool,
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    tool_names = [tool["function"]["name"] for tool in result.tools]
    assert result.debug_metadata["strategy"] == "openai_responses_full_inventory"
    assert "search_tools" not in tool_names
    assert sanitize_mcp_tool_name("github", "search/issues") in tool_names
    assert result.request_kwargs["parallel_tool_calls"] is True


def test_prepare_tool_exposure_strips_controller_search_tool_for_responses() -> None:
    controller_search_schema = {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_TOOL.name,
            "description": SEARCH_TOOLS_TOOL.description,
            "parameters": SEARCH_TOOLS_TOOL.parameters,
        },
    }

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem")],
        controller_tool_schemas=[controller_search_schema],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert all(tool["function"]["name"] != "search_tools" for tool in result.tools)


def test_prepare_tool_exposure_respects_responses_rollout_off(monkeypatch) -> None:
    monkeypatch.setenv("COGNIS_OPENAI_RESPONSES_MODE", "off")
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("github", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
    )
    controller_search_schema = {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_TOOL.name,
            "description": SEARCH_TOOLS_TOOL.description,
            "parameters": SEARCH_TOOLS_TOOL.parameters,
        },
    }

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[controller_search_schema],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4", supports_tool_search=True, supports_responses_api=True, max_tools=3
        ),
        discovered_tool_ids={stable_tool_id(mcp_tool)},
    )

    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert any(tool["function"]["name"] == "search_tools" for tool in result.tools)
