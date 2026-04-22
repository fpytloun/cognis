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
    assert result.request_kwargs["disable_parallel_tool_use"] is False
    cache_control_tools = [
        tool for tool in result.tools if tool.get("function", {}).get("cache_control") is not None
    ]
    assert len(cache_control_tools) == 1
    assert cache_control_tools[0]["function"]["name"] == "read"
    assert cache_control_tools[0]["function"]["cache_control"] == {"type": "ephemeral"}
    deferred = [tool for tool in result.tools if tool["function"]["name"].startswith("mcp_")]
    assert deferred[0]["function"]["defer_loading"] is True
    assert "x-stable-tool-id" not in cache_control_tools[0]["function"]


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
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _tool("bash", source_type="executor", category="shell"),
    ]

    result = prepare_tool_exposure(
        inventory_tools=inventory,
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    tool_names = [tool["function"]["name"] for tool in result.tools]
    assert result.debug_metadata["strategy"] == "openai_responses_full_inventory"
    assert "search_tools" not in tool_names
    assert {"read", "bash"} <= set(tool_names)
    assert result.request_kwargs["parallel_tool_calls"] is True


def test_prepare_tool_exposure_treats_skill_tools_as_deferred() -> None:
    skill_tool = ToolDefinition(
        name="skill_release",
        description="release",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="git-release"),
        category="skill",
    )

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), skill_tool],
        controller_tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": SEARCH_TOOLS_TOOL.name,
                    "description": SEARCH_TOOLS_TOOL.description,
                    "parameters": SEARCH_TOOLS_TOOL.parameters,
                },
            }
        ],
        model="gpt-4o-mini",
        model_info=ModelInfo(model_id="gpt-4o-mini", max_tools=3),
        discovered_tool_ids=set(),
    )

    tool_names = [tool["function"]["name"] for tool in result.tools]
    assert result.debug_metadata["deferred_tool_count"] == 1
    assert "skill_release" not in tool_names
    assert "search_tools" in tool_names


def test_prepare_tool_exposure_marks_skill_tools_deferred_for_responses() -> None:
    skill_tool = ToolDefinition(
        name="skill_release",
        description="release",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="git-release"),
        category="skill",
    )

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), skill_tool],
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    skill_schema = next(
        child
        for tool in result.tools
        if tool["type"] == "namespace"
        for child in tool["tools"]
        if child["name"] == "skill_release"
    )
    assert skill_schema["defer_loading"] is True


def test_prepare_tool_exposure_uses_openai_tool_search_with_deferred_namespaces() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("github", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    namespace = next(tool for tool in result.tools if tool["type"] == "namespace")
    deferred_schema = next(
        child
        for child in namespace["tools"]
        if child["name"] == sanitize_mcp_tool_name("github", "search/issues")
    )
    assert result.debug_metadata["strategy"] == "openai_responses_tool_search"
    assert namespace["name"] == "mcp_github"
    assert deferred_schema["defer_loading"] is True
    assert {tool["type"] for tool in result.tools} >= {"tool_search", "namespace"}
    assert result.request_kwargs["tool_choice"]["type"] == "allowed_tools"
    assert result.request_kwargs["tool_choice"]["mode"] == "auto"
    assert all(tool["type"] == "function" for tool in result.request_kwargs["tool_choice"]["tools"])


def test_prepare_tool_exposure_uses_controller_search_fallback_when_allowed_tools_unsupported() -> (
    None
):
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
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=False,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "openai_responses_controller_search_fallback"
    assert result.request_kwargs["tool_choice"] == "auto"
    assert any(
        tool.get("function", {}).get("name") == SEARCH_TOOLS_TOOL.name for tool in result.tools
    )
    assert all(tool["type"] != "namespace" for tool in result.tools)


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
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    skill_schema = next(
        child
        for tool in result.tools
        if tool["type"] == "namespace"
        for child in tool["tools"]
        if child["name"] == "run_release_now"
    )
    assert skill_schema["defer_loading"] is True


def test_prepare_tool_exposure_openai_tool_search_updates_alias_map_for_namespace_children() -> (
    None
):
    skill_tool = ToolDefinition(
        name="skill_git-release__run_release",
        description="release",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="git-release", raw_tool_name="run/release now"),
        category="skill",
    )

    result = prepare_tool_exposure(
        inventory_tools=[skill_tool],
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert result.alias_map["run_release_now"] == "skill_git-release__run_release"


def test_prepare_tool_exposure_prioritizes_discovered_skill_tools_when_slots_are_tight() -> None:
    skill_tool = ToolDefinition(
        name="skill_git-release__run_release",
        description="release",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="skill", skill_id="git-release", raw_tool_name="run_release"),
        category="skill",
    )
    inventory = [
        _tool("read", source_type="executor", category="filesystem"),
        _tool("bash", source_type="executor", category="shell"),
        skill_tool,
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
        discovered_tool_ids={stable_tool_id(skill_tool)},
    )

    tool_names = [tool["function"]["name"] for tool in result.tools]
    assert "run_release" in tool_names


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
        inventory_tools=[
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
        ],
        controller_tool_schemas=[controller_search_schema],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert all(tool.get("function", {}).get("name") != "search_tools" for tool in result.tools)
    assert any(tool["type"] == "tool_search" for tool in result.tools)


def test_prepare_tool_exposure_strips_controller_search_tool_for_responses_full_inventory() -> None:
    controller_search_schema = {
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS_TOOL.name,
            "description": SEARCH_TOOLS_TOOL.description,
            "parameters": SEARCH_TOOLS_TOOL.parameters,
        },
    }

    result = prepare_tool_exposure(
        inventory_tools=[
            _tool("read", source_type="executor", category="filesystem"),
            _tool("bash", source_type="executor", category="shell"),
        ],
        controller_tool_schemas=[controller_search_schema],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "openai_responses_full_inventory"
    assert all(tool.get("function", {}).get("name") != "search_tools" for tool in result.tools)


def test_prepare_tool_exposure_dedupes_openai_namespace_names() -> None:
    tool_a = ToolDefinition(
        name=sanitize_mcp_tool_name("srv/a", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="srv/a", raw_tool_name="search/issues"),
        category="mcp",
    )
    tool_b = ToolDefinition(
        name=sanitize_mcp_tool_name("srv_a", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="local_mcp", server_name="srv_a", raw_tool_name="search/issues"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[tool_a, tool_b],
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    namespace_names = [tool["name"] for tool in result.tools if tool["type"] == "namespace"]
    assert len(namespace_names) == len(set(namespace_names))


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
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=True,
            max_tools=3,
        ),
        discovered_tool_ids={stable_tool_id(mcp_tool)},
    )

    assert result.debug_metadata["strategy"] == "generic_search_tools"
    assert any(tool["function"]["name"] == "search_tools" for tool in result.tools)


def test_prepare_tool_exposure_uses_flat_tool_search_responses_when_namespace_tools_unsupported() -> (
    None
):
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("github", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=True,
            supports_responses_api=True,
            supports_openai_namespace_tools=False,
            supports_openai_allowed_tools=True,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "openai_responses_flat_tool_search"
    assert all(tool["type"] != "namespace" for tool in result.tools)
    deferred_schema = next(
        tool
        for tool in result.tools
        if tool.get("function", {}).get("name") == sanitize_mcp_tool_name("github", "search/issues")
    )
    assert deferred_schema["function"]["defer_loading"] is True
    assert any(tool["type"] == "tool_search" for tool in result.tools)
    assert result.request_kwargs["tool_choice"]["type"] == "allowed_tools"
    assert result.request_kwargs["tool_choice"]["mode"] == "auto"
    assert all(tool["type"] == "function" for tool in result.request_kwargs["tool_choice"]["tools"])


def test_prepare_tool_exposure_drops_defer_loading_without_native_tool_search() -> None:
    mcp_tool = ToolDefinition(
        name=sanitize_mcp_tool_name("github", "search/issues"),
        description="search",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="intaris_mcp", server_name="github", raw_tool_name="search/issues"),
        category="mcp",
    )

    result = prepare_tool_exposure(
        inventory_tools=[_tool("read", source_type="executor", category="filesystem"), mcp_tool],
        controller_tool_schemas=[],
        model="gpt-5.4",
        model_info=ModelInfo(
            model_id="gpt-5.4",
            supports_tool_search=False,
            supports_responses_api=True,
            supports_openai_namespace_tools=False,
            max_tools=128,
        ),
        discovered_tool_ids=set(),
    )

    assert result.debug_metadata["strategy"] == "openai_responses_full_inventory_no_defer"
    deferred_schema = next(
        tool
        for tool in result.tools
        if tool.get("function", {}).get("name") == sanitize_mcp_tool_name("github", "search/issues")
    )
    assert "defer_loading" not in deferred_schema["function"]
    assert all(tool["type"] != "tool_search" for tool in result.tools)
