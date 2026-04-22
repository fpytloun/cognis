from __future__ import annotations

from cognis.models.tool import ToolDefinition, ToolSource
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL, search_inventory


def _tool(name: str, description: str, category: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category=category,
        read_only=True,
    )


def test_search_inventory_returns_ranked_permission_filtered_matches() -> None:
    tools = [
        SEARCH_TOOLS_TOOL,
        _tool("read", "Read a file from disk", "filesystem"),
        _tool("glob", "Find files by pattern", "filesystem"),
        _tool("bash", "Run shell commands", "shell"),
    ]

    matches = search_inventory(tools, "read file", category="filesystem", limit=5)

    assert [match["name"] for match in matches] == ["read", "glob"]
    assert all(match["category"] == "filesystem" for match in matches)
    assert all(match["name"] != SEARCH_TOOLS_TOOL.name for match in matches)


def test_search_inventory_limits_results() -> None:
    tools = [_tool(f"tool_{index}", "Search helper", "system") for index in range(30)]

    matches = search_inventory(tools, "search", limit=50)

    assert len(matches) == 20


def test_search_inventory_accepts_profile_group_alias_categories() -> None:
    tools = [
        _tool("skill_load", "Load a named skill", "skill"),
        _tool("read_tool_output", "Read saved tool output by call id", "context"),
        _tool("bash", "Run shell commands", "shell"),
    ]

    skill_matches = search_inventory(tools, "load skill", category="system", limit=5)
    output_matches = search_inventory(
        tools,
        "read saved output",
        category="system",
        limit=5,
    )

    assert [match["name"] for match in skill_matches] == ["skill_load"]
    assert [match["name"] for match in output_matches] == ["read_tool_output"]
    assert skill_matches[0]["profile_group"] == "system"
    assert output_matches[0]["profile_group"] == "system"
