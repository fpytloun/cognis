"""Built-in tools for exploring full tool outputs.

When tool results are truncated or pruned from the LLM context, the
LLM can use these tools to read or search the full output stored on
disk by the :class:`~cognis.core.tool_output_store.ToolOutputStore`.
"""

from __future__ import annotations

from typing import Any

from cognis.core.tool_output_store import ToolOutputStore
from cognis.models.tool import ToolDefinition, ToolResult, ToolSource

_SOURCE = ToolSource(type="builtin")

TOOL_OUTPUT_TOOL_NAMES = frozenset({"read_tool_output", "search_tool_output"})

READ_TOOL_OUTPUT = ToolDefinition(
    name="read_tool_output",
    description=(
        "Read the full output of a previous tool call by its call_id. "
        "Use when a tool result was truncated or cleared from context. "
        "Supports pagination via offset (1-indexed line number) and limit. "
        "Returns line-numbered content similar to the file read tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": "The call_id of the tool result to read.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start from (1-indexed). Default: 1.",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum lines to return. Default: 200.",
                "default": 200,
            },
        },
        "required": ["call_id"],
    },
    source=_SOURCE,
    category="context",
    read_only=True,
    timeout_seconds=10,
    max_result_size=50_000,
)

SEARCH_TOOL_OUTPUT = ToolDefinition(
    name="search_tool_output",
    description=(
        "Search within the full output of a previous tool call using a "
        "regex pattern. Returns matching lines with surrounding context. "
        "Use when you need to find specific content in a large or cleared "
        "tool output."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": "The call_id of the tool result to search.",
            },
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for (case-insensitive).",
            },
            "context_lines": {
                "type": "integer",
                "description": "Lines of context around each match. Default: 3.",
                "default": 3,
            },
        },
        "required": ["call_id", "pattern"],
    },
    source=_SOURCE,
    category="context",
    read_only=True,
    timeout_seconds=10,
    max_result_size=50_000,
)


def tool_output_tools() -> list[ToolDefinition]:
    """Return tool output exploration tool definitions."""
    return [READ_TOOL_OUTPUT, SEARCH_TOOL_OUTPUT]


def is_tool_output_tool(name: str) -> bool:
    """Check if a tool name is a tool output exploration tool."""
    return name in TOOL_OUTPUT_TOOL_NAMES


async def handle_tool_output_tool(
    tool_name: str,
    arguments: dict[str, Any],
    store: ToolOutputStore,
) -> ToolResult:
    """Dispatch a tool output exploration call."""

    if tool_name == "read_tool_output":
        return await _handle_read(arguments, store)
    if tool_name == "search_tool_output":
        return await _handle_search(arguments, store)
    return ToolResult(output=f"Unknown tool output tool: {tool_name}", is_error=True)


async def _handle_read(arguments: dict[str, Any], store: ToolOutputStore) -> ToolResult:
    call_id = arguments.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return ToolResult(output="call_id is required.", is_error=True)

    offset = int(arguments.get("offset", 1))
    limit = int(arguments.get("limit", 200))

    result = await store.read(call_id, offset=offset, limit=limit)
    if result is None:
        return ToolResult(
            output=f"No stored output found for call_id '{call_id}'. "
            "The output may have expired or the call_id may be incorrect.",
            is_error=True,
        )

    lines: list[str] = [result.content]
    if result.has_more:
        next_offset = offset + limit
        lines.append(
            f"\n(Showing lines {offset}-{offset + limit - 1} of {result.total_lines}. "
            f"Use offset={next_offset} to continue.)"
        )
    else:
        lines.append(f"\n(Total: {result.total_lines} lines)")

    return ToolResult(output="\n".join(lines))


async def _handle_search(arguments: dict[str, Any], store: ToolOutputStore) -> ToolResult:
    call_id = arguments.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return ToolResult(output="call_id is required.", is_error=True)

    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return ToolResult(output="pattern is required.", is_error=True)

    context_lines = int(arguments.get("context_lines", 3))

    result = await store.search(call_id, pattern, context_lines=context_lines)
    if result is None:
        return ToolResult(
            output=f"No stored output found for call_id '{call_id}'.",
            is_error=True,
        )

    if not result.matches:
        return ToolResult(output=f"No matches found for pattern '{pattern}'.")

    parts: list[str] = []
    for match in result.matches:
        section: list[str] = []
        for ctx_line in match.context_before:
            section.append(f"  {ctx_line}")
        section.append(f"  {match.line_number}: {match.line}  <-- match")
        for ctx_line in match.context_after:
            section.append(f"  {ctx_line}")
        parts.append("\n".join(section))

    header = f"Found {result.total_matches} match(es) for '{pattern}'"
    if result.truncated:
        header += f" (showing first {len(result.matches)})"
    header += ":"

    return ToolResult(output=header + "\n\n" + "\n---\n".join(parts))
