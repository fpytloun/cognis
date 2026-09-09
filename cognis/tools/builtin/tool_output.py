"""Built-in tools for exploring full tool outputs.

When tool results are truncated or pruned from the LLM context, the
LLM can use these tools to read or search the full output stored on
disk by the :class:`~cognis.core.tool_output_store.ToolOutputStore`.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from prometheus_client import Counter

from cognis.core.agent_registry import AgentRegistry
from cognis.core.historical_tool_output import resolve_historical_tool_event, tool_event_storage_id
from cognis.core.tool_output_store import ToolOutputStore
from cognis.logging import get_logger
from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolResult, ToolSource

logger = get_logger(__name__)
_SOURCE = ToolSource(type="builtin")
_NORMAL_RECOVERY_OUTPUT_CAP = 50_000
_RECOVERY_OUTPUT_CAPS = {
    "normal": _NORMAL_RECOVERY_OUTPUT_CAP,
    "pressure": 25_000,
    "critical": 12_000,
}
TOOL_OUTPUT_RECOVERY_CLAMPS_TOTAL = Counter(
    "cognis_tool_output_recovery_clamps_total",
    "Tool-output recovery result slices clamped because of context pressure.",
    labelnames=("mode", "tool"),
)

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
        "event; never invent or use placeholder values. For historical content, pass "
        "reference from read_conversation_messages and part=arguments (tool_call) or "
        "result (tool_result). Persisted arguments can be bounded, not original exact arguments."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reference": {
                "type": "object",
                "description": "Historical event locator, verified again on every read.",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "seq": {"type": "integer", "minimum": 1},
                    "kind": {"type": "string", "enum": ["tool_call", "tool_result"]},
                    "call_id": {"type": "string"},
                },
                "required": ["conversation_id", "session_id", "seq", "kind", "call_id"],
                "additionalProperties": False,
            },
            "part": {"type": "string", "enum": ["arguments", "result"], "default": "result"},
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
    """Metadata that records which source output a helper result came from."""

    return {
        "source_call_id": call_id,
        "original_size": len(output),
    }


async def handle_tool_output_tool(
    tool_name: str,
    arguments: dict[str, Any],
    store: ToolOutputStore | None,
    *,
    pressure_mode: Any = None,
    session_factory: Any = None,
    intaris: Any = None,
    user_email: str | None = None,
) -> ToolResult:
    """Dispatch a tool output exploration call."""

    if tool_name == "read_tool_output" and "reference" in arguments:
        return await _handle_historical_read(
            arguments,
            store,
            session_factory=session_factory,
            intaris=intaris,
            user_email=user_email,
            pressure_mode=pressure_mode,
        )
    if store is None:
        return ToolResult(output="Tool output store not available.", is_error=True)
    if tool_name == "read_tool_output":
        if arguments.get("part", "result") != "result":
            return ToolResult(
                output="Historical reference is required for arguments.", is_error=True
            )
        return await _handle_read(arguments, store, pressure_mode=pressure_mode)
    if tool_name == "search_tool_output":
        return await _handle_search(arguments, store, pressure_mode=pressure_mode)
    if tool_name == "list_tool_output_anchors":
        return await _handle_list_anchors(arguments, store)
    if tool_name == "read_tool_output_anchor":
        return await _handle_read_anchor(arguments, store, pressure_mode=pressure_mode)
    return ToolResult(output=f"Unknown tool output tool: {tool_name}", is_error=True)


async def _handle_historical_read(
    arguments: dict[str, Any],
    store: ToolOutputStore | None,
    *,
    session_factory: Any,
    intaris: Any,
    user_email: str | None,
    pressure_mode: Any,
) -> ToolResult:
    if session_factory is None or intaris is None or not user_email:
        return ToolResult(output="Historical reader is unavailable.", is_error=True)
    call_id = arguments.get("call_id")
    part = arguments.get("part", "result")
    reference = arguments["reference"]
    if (
        not isinstance(call_id, str)
        or not call_id
        or part not in ("arguments", "result")
        or not isinstance(reference, dict)
        or reference.get("kind") != ("tool_call" if part == "arguments" else "tool_result")
    ):
        return ToolResult(output="Reference kind must match the requested part.", is_error=True)
    try:
        offset = max(1, int(arguments.get("offset", 1)))
        limit = min(2000, max(1, int(arguments.get("limit", 200))))
        data = await resolve_historical_tool_event(
            reference,
            call_id=call_id,
            user_email=user_email,
            session_factory=session_factory,
            registry=AgentRegistry(session_factory),
            intaris=intaris,
        )
    except (ValueError, TypeError):
        return ToolResult(output="Historical event not found or unavailable.", is_error=True)
    metadata: dict[str, Any] = {
        "reference": reference,
        "part": part,
        "content_trust": "untrusted",
    }
    if part == "result" and data.get("has_full_output") and store is not None:
        storage_id = tool_event_storage_id(data, call_id)
        if storage_id is None:
            return ToolResult(output="Historical event not found or unavailable.", is_error=True)
        stored = await store.read(storage_id, offset=offset, limit=limit)
        if stored is not None:
            metadata.update(
                source="stored_output",
                source_call_id=storage_id,
            )
            return _historical_page(
                stored.content.splitlines(),
                offset=stored.offset,
                limit=stored.limit,
                total_lines=stored.total_lines,
                metadata=metadata,
                notice="",
                pressure_mode=pressure_mode,
            )
        metadata["store_status"] = "missing_or_expired"
    elif part == "result" and store is None:
        metadata["store_status"] = "unavailable"
    value = data.get("arguments" if part == "arguments" else "result")
    if value is None:
        metadata.update(source="unavailable", available=False)
        return ToolResult(
            output="Requested persisted content is unavailable.", metadata=metadata, is_error=True
        )
    decoded = value
    if part == "arguments" and isinstance(value, str):
        with contextlib.suppress(ValueError):
            decoded = json.loads(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    lines = text.splitlines() or [""]
    selected = lines[offset - 1 : offset - 1 + limit]
    numbered = [f"{offset + i}: {line}" for i, line in enumerate(selected)]
    source = "persisted_arguments" if part == "arguments" else "event_preview"
    persisted_truncated = (
        bool(decoded.get("_truncated"))
        if part == "arguments" and isinstance(decoded, dict)
        else bool(data.get("truncated") or data.get("agent_visible_truncated"))
    )
    metadata.update(
        source=source,
        available=True,
        persisted_truncated=persisted_truncated,
    )
    if part == "arguments":
        metadata["original_arguments_exact"] = False
    notice = (
        "Persisted arguments (possibly bounded; not original exact arguments)."
        if part == "arguments"
        else "Persisted event preview (not recovered full output)."
    )
    return _historical_page(
        numbered,
        offset=offset,
        limit=limit,
        total_lines=len(lines),
        metadata=metadata,
        notice=notice,
        pressure_mode=pressure_mode,
    )


def _historical_page(
    lines: list[str],
    *,
    offset: int,
    limit: int,
    total_lines: int,
    metadata: dict[str, Any],
    notice: str,
    pressure_mode: Any,
) -> ToolResult:
    """Apply character pressure before choosing the next unconsumed line."""
    budget = _RECOVERY_OUTPUT_CAPS[_normalize_pressure_mode(pressure_mode)] - 1000
    retained: list[str] = []
    used = len(notice)
    partial_line = False
    for line in lines:
        if used + len(line) + 1 > budget:
            if not retained:
                retained.append(line[: max(0, budget - used)])
                partial_line = True
            break
        retained.append(line)
        used += len(line) + 1
    consumed = len(retained)
    has_more = offset - 1 + consumed < total_lines
    metadata.update(
        offset=offset,
        limit=limit,
        total_lines=total_lines,
        returned_lines=consumed,
        has_more=has_more,
        next_offset=offset + consumed if has_more else None,
        content_truncated=partial_line,
        page_limited=len(retained) < len(lines),
    )
    parts = [notice] if notice else []
    parts.extend(retained)
    if partial_line:
        parts.append(
            "[This line exceeds the character limit and is truncated. "
            "Line pagination cannot recover the omitted part of this line.]"
        )
    if has_more:
        parts.append(f"[Continue with offset={offset + consumed}.]")
    return ToolResult(output="\n".join(parts), metadata=metadata)


def _normalize_pressure_mode(value: Any) -> str:
    raw = getattr(value, "value", value)
    if isinstance(raw, str) and raw in _RECOVERY_OUTPUT_CAPS:
        return raw
    return "normal"


def _apply_pressure_recovery_clamp(
    output: str,
    *,
    pressure_mode: Any,
    tool_name: str,
    guidance: str,
) -> str:
    mode = _normalize_pressure_mode(pressure_mode)
    cap = _RECOVERY_OUTPUT_CAPS[mode]
    if cap >= _NORMAL_RECOVERY_OUTPUT_CAP or len(output) <= cap:
        return output

    notice = (
        f"\n\n[Tool output recovery truncated under {mode} context pressure. "
        f"Returned at most {cap} characters. {guidance}]"
    )
    content_cap = max(0, cap - len(notice))
    TOOL_OUTPUT_RECOVERY_CLAMPS_TOTAL.labels(mode=mode, tool=tool_name).inc()
    logger.info(
        "tool_output: pressure-aware recovery clamp applied",
        extra={
            "extra_data": {
                "tool_name": tool_name,
                "pressure_mode": mode,
                "cap_chars": cap,
                "output_chars": len(output),
            }
        },
    )
    return output[:content_cap].rstrip() + notice


async def _handle_read(
    arguments: dict[str, Any],
    store: ToolOutputStore,
    *,
    pressure_mode: Any = None,
) -> ToolResult:
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

    output = _apply_pressure_recovery_clamp(
        "\n".join(lines),
        pressure_mode=pressure_mode,
        tool_name="read_tool_output",
        guidance="Use offset/limit to request a smaller slice, or list_tool_output_anchors/read_tool_output_anchor for structured sections.",
    )
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))


async def _handle_search(
    arguments: dict[str, Any],
    store: ToolOutputStore,
    *,
    pressure_mode: Any = None,
) -> ToolResult:
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

    output = _apply_pressure_recovery_clamp(
        header + "\n\n" + "\n---\n".join(parts),
        pressure_mode=pressure_mode,
        tool_name="search_tool_output",
        guidance="Use a narrower pattern, smaller context_lines, or anchors for more focused recovery.",
    )
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
        locator = item.locator or {}
        location = (
            f"lines {item.start_line}-{item.end_line}"
            if item.start_line is not None and item.end_line is not None
            else json.dumps(locator, ensure_ascii=False, sort_keys=True)
        )
        lines.append(f"- {item.anchor} ({item.format}/{item.kind}, {location}){label_suffix}")
    output = "\n".join(lines)
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))


async def _handle_read_anchor(
    arguments: dict[str, Any],
    store: ToolOutputStore,
    *,
    pressure_mode: Any = None,
) -> ToolResult:
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

    locator = result.anchor.locator or {}
    location = (
        f"lines {result.anchor.start_line}-{result.anchor.end_line}"
        if result.anchor.start_line is not None and result.anchor.end_line is not None
        else json.dumps(locator, ensure_ascii=False, sort_keys=True)
    )
    header = (
        f"Anchor '{result.anchor.anchor}' ({result.anchor.format}/{result.anchor.kind}, {location})"
    )
    if result.anchor.label:
        header += f" - {result.anchor.label}"
    output = _apply_pressure_recovery_clamp(
        header + "\n\n" + result.content,
        pressure_mode=pressure_mode,
        tool_name="read_tool_output_anchor",
        guidance="Use before_lines/after_lines to request less surrounding context, or read neighboring anchors separately.",
    )
    return ToolResult(output=output, metadata=_recovery_metadata(call_id, output))
