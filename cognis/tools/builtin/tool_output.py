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

TOOL_OUTPUT_TOOL_NAMES = frozenset(
    {
        "read_tool_output",
        "search_tool_output",
        "list_tool_output_anchors",
        "read_tool_output_anchor",
    }
)

READ_TOOL_OUTPUT = ToolDefinition(
    name="read_tool_output",
    description=(
        "Read the full output of a previous tool call by its call_id. "
        "Use when a tool result was truncated or cleared from context and you "
        "need the omitted sections in order. Supports pagination via offset "
        "(1-indexed line number) and limit. Returns line-numbered content "
        "similar to the file read tool. For structured outputs with anchors, "
        "prefer list_tool_output_anchors and read_tool_output_anchor first. "
        "Only call this when you have a real call_id from a prior tool_call "
        "event; never invent or use placeholder values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": (
                    "Exact call_id string from a prior tool_call event. "
                    "Must not be empty or a placeholder such as 'dummy'."
                ),
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
        "Use this before read_tool_output when you need to locate a specific "
        "error, URL, symbol, heading, date, or keyword inside a large or "
        "cleared tool output. Only call this when you have a real call_id "
        "from a prior tool_call event; never invent or use placeholder "
        "values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": (
                    "Exact call_id string from a prior tool_call event. "
                    "Must not be empty or a placeholder such as 'dummy'."
                ),
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

LIST_TOOL_OUTPUT_ANCHORS = ToolDefinition(
    name="list_tool_output_anchors",
    description=(
        "List named anchors for a previous tool output when it contains structured "
        "sections such as search results. Use this before read_tool_output_anchor "
        "when you need to inspect a specific saved section without regex search. "
        "Only call this when you have a real call_id from a prior tool_call "
        "event; never invent or use placeholder values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": (
                    "Exact call_id string from a prior tool_call event. "
                    "Must not be empty or a placeholder such as 'dummy'."
                ),
            }
        },
        "required": ["call_id"],
    },
    source=_SOURCE,
    category="context",
    read_only=True,
    timeout_seconds=10,
    max_result_size=20_000,
)

READ_TOOL_OUTPUT_ANCHOR = ToolDefinition(
    name="read_tool_output_anchor",
    description=(
        "Read a named anchored section from a previous tool output. Use this for "
        "structured outputs such as saved search results when you want one section "
        "without reloading the entire output. Only call this when you have a "
        "real call_id from a prior tool_call event; never invent or use "
        "placeholder values."
    ),
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": (
                    "Exact call_id string from a prior tool_call event. "
                    "Must not be empty or a placeholder such as 'dummy'."
                ),
            },
            "anchor": {
                "type": "string",
                "description": "Exact anchor name to read, e.g. 'result:3'.",
            },
            "before_lines": {
                "type": "integer",
                "description": "Optional lines to include before the anchored section.",
                "default": 0,
            },
            "after_lines": {
                "type": "integer",
                "description": "Optional lines to include after the anchored section.",
                "default": 0,
            },
        },
        "required": ["call_id", "anchor"],
    },
    source=_SOURCE,
    category="context",
    read_only=True,
    timeout_seconds=10,
    max_result_size=30_000,
)


def tool_output_tools() -> list[ToolDefinition]:
    """Return tool output exploration tool definitions."""
    return [
        READ_TOOL_OUTPUT,
        SEARCH_TOOL_OUTPUT,
        LIST_TOOL_OUTPUT_ANCHORS,
        READ_TOOL_OUTPUT_ANCHOR,
    ]


def is_tool_output_tool(name: str) -> bool:
    """Check if a tool name is a tool output exploration tool."""
    return name in TOOL_OUTPUT_TOOL_NAMES


def _recovery_metadata(call_id: str, output: str) -> dict[str, Any]:
    """Metadata that lets compacted helper results point back to the source output."""

    return {
        "recovery_call_id": call_id,
        "original_size": len(output),
    }


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
    if tool_name == "list_tool_output_anchors":
        return await _handle_list_anchors(arguments, store)
    if tool_name == "read_tool_output_anchor":
        return await _handle_read_anchor(arguments, store)
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

    output = "\n".join(lines)
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))


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
        output = f"No matches found for pattern '{pattern}'."
        return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))

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

    output = header + "\n\n" + "\n---\n".join(parts)
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))


async def _handle_list_anchors(arguments: dict[str, Any], store: ToolOutputStore) -> ToolResult:
    call_id = arguments.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return ToolResult(output="call_id is required.", is_error=True)

    anchors = await store.list_anchors(call_id)
    if anchors is None:
        return ToolResult(
            output=f"No stored output found for call_id '{call_id}'.",
            is_error=True,
        )
    if not anchors:
        output = f"No anchors found for call_id '{call_id}'."
        return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))

    lines = [f"Found {len(anchors)} anchor(s) for '{call_id}':", ""]
    for item in anchors:
        label_suffix = f" - {item.label}" if item.label else ""
        lines.append(
            f"- {item.anchor} ({item.kind}, lines {item.start_line}-{item.end_line}){label_suffix}"
        )
    output = "\n".join(lines)
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))


async def _handle_read_anchor(arguments: dict[str, Any], store: ToolOutputStore) -> ToolResult:
    call_id = arguments.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return ToolResult(output="call_id is required.", is_error=True)

    anchor = arguments.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return ToolResult(output="anchor is required.", is_error=True)

    before_lines = max(0, int(arguments.get("before_lines", 0)))
    after_lines = max(0, int(arguments.get("after_lines", 0)))
    result = await store.read_anchor(
        call_id,
        anchor,
        before_lines=before_lines,
        after_lines=after_lines,
    )
    if result is None:
        anchors = await store.list_anchors(call_id)
        if anchors is None:
            return ToolResult(
                output=f"No stored output found for call_id '{call_id}'.",
                is_error=True,
            )
        available = ", ".join(item.anchor for item in anchors[:10])
        message = f"No anchor named '{anchor}' found for call_id '{call_id}'."
        if available:
            message += f" Available anchors: {available}."
        return ToolResult(output=message, is_error=True)

    header = (
        f"Anchor '{result.anchor.anchor}' ({result.anchor.kind}, "
        f"lines {result.anchor.start_line}-{result.anchor.end_line})"
    )
    if result.anchor.label:
        header += f" - {result.anchor.label}"
    output = header + "\n\n" + result.content
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))
