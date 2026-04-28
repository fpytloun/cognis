from __future__ import annotations

from cognis.core.tool_retrieval import retrieve_relevant_skills, retrieve_relevant_tools
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
    assert matches[0]["handle"]["tool_id"] == "builtin:read"
    assert matches[0]["handle"]["callable_name"] == "read"
    assert matches[0]["handle"]["scope"] == "session"
    assert matches[0]["handle"]["permission_scope"] == "current_session_effective_inventory"


def test_search_inventory_limits_results() -> None:
    tools = [_tool(f"tool_{index}", "Search helper", "system") for index in range(30)]

    matches = search_inventory(tools, "search", limit=50)

    assert len(matches) == 20


def test_search_inventory_omits_already_visible_tools() -> None:
    tools = [
        _tool("get_cart", "Get current cart contents", "mcp"),
        _tool("repeat_order", "Repeat a previous order", "mcp"),
    ]

    matches = search_inventory(
        tools,
        "repeat order",
        category="mcp",
        already_visible_tool_ids={"builtin:get_cart"},
        limit=5,
    )

    assert [match["name"] for match in matches] == ["repeat_order"]


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

    assert skill_matches[0]["name"] == "skill_load"
    assert output_matches[0]["name"] == "read_tool_output"
    assert skill_matches[0]["profile_group"] == "system"
    assert output_matches[0]["profile_group"] == "system"


def test_search_inventory_treats_category_as_hint_not_hard_filter() -> None:
    tools = [
        _tool("bash", "Run shell commands in a terminal", "shell"),
        _tool("read_tool_output", "Read saved tool output", "context"),
    ]

    matches = search_inventory(
        tools,
        "bash shell command execution terminal tool",
        category="system",  # wrong hint from the model
        limit=5,
    )

    assert matches
    assert matches[0]["name"] == "bash"


def test_search_inventory_category_hint_still_boosts_matching_tools() -> None:
    tools = [
        _tool("bash", "Run shell commands in a terminal", "shell"),
        _tool("task_list", "List system tasks and runtime state", "system"),
    ]

    matches = search_inventory(
        tools,
        "tasks",
        category="system",
        limit=5,
    )

    assert matches
    assert matches[0]["name"] == "task_list"


def test_search_inventory_uses_bm25_for_multi_term_mcp_queries() -> None:
    tools = [
        _tool(
            "mcp_googleworkspace__search_messages",
            "Search Gmail messages and calendar events in Google Workspace",
            "mcp",
        ),
        _tool(
            "mcp_rohlik__fetch_orders",
            "Retrieve delivered and upcoming grocery orders",
            "mcp",
        ),
        _tool(
            "mcp_todoist__find_tasks",
            "Find Todoist tasks by text and date",
            "mcp",
        ),
    ]

    matches = search_inventory(
        tools,
        "Google Workspace tools for calendar events and Gmail search content retrieval",
        category="mcp",
        limit=5,
    )

    assert matches[0]["name"] == "mcp_googleworkspace__search_messages"


def test_retrieve_relevant_skills_returns_exact_skill_match() -> None:
    skills = [
        {
            "skill_id": "skill_daily_brief",
            "name": "daily-brief",
            "description": "Build a Czech morning briefing with agenda, news, markets, and weather.",
            "tags": ["briefing", "news"],
        },
        {
            "skill_id": "skill_shopping",
            "name": "rohlik-smart-shopping",
            "description": "Handle smart grocery shopping tasks.",
            "tags": ["shopping"],
        },
    ]

    matches = retrieve_relevant_skills(
        "Load and run the daily-brief skill for today's morning briefing",
        skills,
        loaded_skill_ids=set(),
    )

    assert len(matches) == 1
    assert matches[0].skill_id == "skill_daily_brief"


def test_retrieve_relevant_tools_drops_already_visible_tools() -> None:
    tools = [
        _tool("image_edit", "Edit image artifacts using a text prompt", "image"),
        _tool("web_search", "Search the web for information", "web"),
    ]

    matches = retrieve_relevant_tools(
        "edit this image",
        tools,
        already_visible_tool_ids={"builtin:image_edit"},
    )

    assert matches == []
