"""Controller-managed tool discovery definitions and helpers."""

from __future__ import annotations

from typing import Any

from cognis.models.tool import ToolDefinition, ToolSource, stable_tool_id, tool_profile_group

SEARCH_TOOLS_TOOL = ToolDefinition(
    name="search_tools",
    description=(
        "Search for additional tools available in this session. "
        "Use when you need a capability not in your current tool set."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query for the tool capability you need.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category or profile-group filter such as mcp, "
                    "filesystem, skill, context, or system."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of matches to return.",
            },
        },
        "required": ["query"],
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)


def search_inventory(
    tools: list[ToolDefinition],
    query: str,
    *,
    category: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search a permission-filtered tool inventory and return ranked matches."""

    normalized_query = query.strip().lower()
    if not normalized_query:
        return []
    normalized_category = category.strip().lower() if isinstance(category, str) else None
    limit = max(1, min(limit, 20))
    query_terms = [term for term in normalized_query.split() if term]
    matches: list[tuple[int, dict[str, Any]]] = []
    for tool in tools:
        if tool.name == SEARCH_TOOLS_TOOL.name:
            continue
        profile_group = tool_profile_group(tool)
        if normalized_category and normalized_category not in {
            tool.category.lower(),
            profile_group.lower(),
        }:
            continue
        display_name = (
            tool.source.raw_tool_name
            if tool.source.type == "skill" and tool.source.raw_tool_name
            else tool.name
        )
        haystack = f"{display_name} {tool.description} {tool.category} {profile_group}".lower()
        score = 0
        if normalized_query in display_name.lower():
            score += 50
        if normalized_query in tool.description.lower():
            score += 25
        if normalized_query in tool.category.lower():
            score += 10
        if normalized_query in profile_group.lower():
            score += 10
        score += sum(5 for term in query_terms if term in haystack)
        if score <= 0:
            continue
        matches.append(
            (
                score,
                {
                    "tool_id": stable_tool_id(tool),
                    "name": display_name,
                    "description": tool.description,
                    "category": tool.category,
                    "profile_group": profile_group,
                    "source": tool.source.model_dump(mode="json"),
                },
            )
        )
    matches.sort(key=lambda item: (-item[0], item[1]["name"], item[1]["tool_id"]))
    return [item for _, item in matches[:limit]]
