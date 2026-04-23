from __future__ import annotations

from cognis.core.tool_exposure import (
    LLMApiMode,
    ToolDiscoveryMode,
    ToolExposureContract,
    prepare_tool_exposure,
)
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


def _mcp(server: str, raw_name: str, *, category: str = "mcp") -> ToolDefinition:
    return ToolDefinition(
        name=sanitize_mcp_tool_name(server, raw_name),
        description=f"{server} {raw_name}",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name=server, raw_tool_name=raw_name),
        category=category,
    )


def _contract(
    *,
    llm_api: LLMApiMode = LLMApiMode.CHAT_COMPLETIONS,
    discovery_mode: ToolDiscoveryMode = ToolDiscoveryMode.CONTROLLER_SEARCH,
) -> ToolExposureContract:
    return ToolExposureContract(llm_api=llm_api, discovery_mode=discovery_mode)


def _search_schema() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_TOOL.name,
            "description": SEARCH_TOOLS_TOOL.description,
            "parameters": SEARCH_TOOLS_TOOL.parameters,
        },
    }


def test_prepare_tool_exposure_uses_anthropic_deferred_loading_with_controller_search() -> None:
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _mcp("github", "search/issues"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude-sonnet-4", supports_defer_loading=True),
        contract=_contract(),
        promoted_tool_ids=set(),
    )

    tool_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "anthropic_defer_loading"
    assert result.debug_metadata["discovery_mode"] == "controller_search"
    assert "search_tools" in tool_names
    deferred = [
        tool for tool in result.tools if tool.get("function", {}).get("name", "").startswith("mcp_")
    ]
    assert deferred[0]["function"]["defer_loading"] is True


def test_prepare_tool_exposure_uses_generic_search_fallback_with_promoted_tools() -> None:
    mcp_tool = _mcp("github", "search/issues")
    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-4o-mini", max_tools=3),
        contract=_contract(),
        promoted_tool_ids={stable_tool_id(mcp_tool)},
    )

    tool_names = [tool["function"]["name"] for tool in result.tools if tool["type"] == "function"]
    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert "search_tools" in tool_names
    assert sanitize_mcp_tool_name("github", "search/issues") in tool_names


def test_prepare_tool_exposure_uses_responses_controller_search_for_openai() -> None:
    mcp_tool = _mcp("github", "search/issues")

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert result.debug_metadata["llm_api"] == "responses"
    assert result.request_kwargs["tool_choice"] == "auto"
    assert all(tool["type"] != "namespace" for tool in result.tools)
    assert all(tool["type"] != "tool_search" for tool in result.tools)
    assert any(tool.get("function", {}).get("name") == "search_tools" for tool in result.tools)


def test_prepare_tool_exposure_responses_visible_only_when_search_disabled() -> None:
    mcp_tool = _mcp("github", "search/issues")

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES, discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        allow_tool_search=False,
    )

    function_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "openai_responses_visible_only"
    assert "search_tools" not in function_names
    assert sanitize_mcp_tool_name("github", "search/issues") not in function_names
    assert function_names == ["read"]


def test_prepare_tool_exposure_keeps_skill_and_tool_output_helpers_visible_under_fallback_cap() -> (
    None
):
    deferred_mcp = _mcp("todoist", "find/tasks")
    inventory = [
        _tool("skill_load", category="skill"),
        _tool("read_tool_output", category="context"),
        _tool("search_tool_output", category="context"),
        _tool("list_tool_output_anchors", category="context"),
        _tool("read_tool_output_anchor", category="context"),
        _tool("bash", source_type="executor", category="shell"),
        _tool("web_search", category="web"),
        deferred_mcp,
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=6),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
    )

    tool_names = [tool["function"]["name"] for tool in result.tools if tool["type"] == "function"]
    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert tool_names[0] == "search_tools"
    assert set(tool_names[1:]) == {
        "skill_load",
        "read_tool_output",
        "search_tool_output",
        "list_tool_output_anchors",
        "read_tool_output_anchor",
    }


def test_promoted_tool_always_surfaces_next_turn_even_when_hidden() -> None:
    """Promoted tools (from search_tools or skill activation) must become visible
    on the next turn regardless of which hidden bucket they came from."""
    get_events = _mcp("googleworkspace", "get_events")
    get_form = _mcp("googleworkspace", "get_form")
    read = _tool("read", source_type="executor", category="filesystem")

    # get_events is hidden (not in default_visible_tool_ids), get_form is also hidden.
    # The model searched and found get_events — it should be promoted visible.
    result = prepare_tool_exposure(
        inventory_tools=[read, get_events, get_form],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events)},
        default_visible_tool_ids={stable_tool_id(read)},
    )

    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert stable_tool_id(get_events) in result.visible_tool_ids
    assert stable_tool_id(get_form) not in result.visible_tool_ids
    assert stable_tool_id(get_events) not in result.hidden_searchable_tool_ids


def test_promoted_tool_wins_slots_over_irrelevant_mcp_under_cap() -> None:
    """Under a tight slot cap, promoted tools must win over arbitrary MCP tools."""
    get_events = _mcp("googleworkspace", "get_events")
    get_form = _mcp("googleworkspace", "get_form")
    get_drive = _mcp("googleworkspace", "get_drive_shareable_link")
    read = _tool("read", source_type="executor", category="filesystem")

    # Tight cap: only 2 inventory slots (controller schemas take the rest).
    # get_events is promoted; get_form and get_drive are not.
    result = prepare_tool_exposure(
        inventory_tools=[read, get_events, get_form, get_drive],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=3),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids={stable_tool_id(get_events)},
        default_visible_tool_ids={stable_tool_id(read)},
    )

    assert stable_tool_id(get_events) in result.visible_tool_ids
    assert stable_tool_id(get_form) not in result.visible_tool_ids
    assert stable_tool_id(get_drive) not in result.visible_tool_ids


def test_hidden_searchable_tool_ids_excludes_visible_tools() -> None:
    """hidden_searchable_tool_ids must not overlap with visible_tool_ids."""
    get_events = _mcp("googleworkspace", "get_events")
    get_form = _mcp("googleworkspace", "get_form")
    read = _tool("read", source_type="executor", category="filesystem")

    result = prepare_tool_exposure(
        inventory_tools=[read, get_events, get_form],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-5.4", supports_responses_api=True, max_tools=128),
        contract=_contract(llm_api=LLMApiMode.RESPONSES),
        promoted_tool_ids=set(),
        default_visible_tool_ids={stable_tool_id(read)},
    )

    assert not result.visible_tool_ids & result.hidden_searchable_tool_ids


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
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude", supports_defer_loading=True),
        contract=_contract(),
        promoted_tool_ids=set(),
    )

    visible_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert len(visible_names) == len(set(visible_names))


def test_prepare_tool_exposure_sanitizes_skill_visible_names() -> None:
    skill_tool = ToolDefinition(
        name="skill_git-release__run_release",
        description="release",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="git-release", raw_tool_name="run/release now"),
        category="skill",
    )

    result = prepare_tool_exposure(
        inventory_tools=[skill_tool],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="gpt-4o-mini", max_tools=8),
        contract=_contract(),
        promoted_tool_ids={stable_tool_id(skill_tool)},
    )

    assert result.alias_map["run_release_now"] == "skill_git-release__run_release"


def test_prepare_tool_exposure_visible_only_chat_without_search() -> None:
    result = prepare_tool_exposure(
        inventory_tools=[
            _tool("read", source_type="executor", category="filesystem"),
            _tool("bash", source_type="executor", category="shell"),
        ],
        controller_tool_schemas=[_search_schema()],
        model_info=ModelInfo(model_id="claude", max_tools=16),
        contract=_contract(discovery_mode=ToolDiscoveryMode.NONE),
        promoted_tool_ids=set(),
        default_visible_tool_ids={
            stable_tool_id(_tool("read", source_type="executor", category="filesystem"))
        },
        allow_tool_search=False,
    )

    function_names = [
        tool.get("function", {}).get("name")
        for tool in result.tools
        if tool.get("type") == "function"
    ]
    assert result.debug_metadata["strategy"] == "chat_visible_only"
    assert function_names == ["read"]
