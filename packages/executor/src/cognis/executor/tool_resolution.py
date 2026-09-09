"""Executor-local tool enablement filtering."""

from __future__ import annotations

from collections.abc import Sequence

from cognis.models.tool import ToolDefinition


def is_tool_enabled(
    tool: ToolDefinition,
    enabled_tools: list[str] | None,
    enabled_tool_groups: list[str] | None,
) -> bool:
    """Return whether an executor is enabled to run one tool."""
    tools = enabled_tools or []
    groups = enabled_tool_groups or []
    return (
        "*" in tools
        or tool.name in tools
        or tool.category in groups
        or (tool.profile_group is not None and tool.profile_group in groups)
    )


def filter_tools_by_executor[ToolDefinitionT: ToolDefinition](
    tools: Sequence[ToolDefinitionT],
    enabled_tools: list[str] | None,
    enabled_tool_groups: list[str] | None,
) -> list[ToolDefinitionT]:
    """Filter tool definitions to only those enabled on an executor."""
    return [tool for tool in tools if is_tool_enabled(tool, enabled_tools, enabled_tool_groups)]
