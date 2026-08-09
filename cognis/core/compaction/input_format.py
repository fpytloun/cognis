"""Event-to-text formatting helpers for compaction prompts."""

from __future__ import annotations

import json
from typing import Any

from cognis.core.attachment_utils import merge_content_and_attachment_note
from cognis.core.message_envelope import render_user_event_content

_MESSAGE_MAX_CHARS = 12_000
_TOOL_CALL_REMAINDER_MAX_CHARS = 1_000
_TOOL_RESULT_MAX_CHARS = 2_000
_IDENTIFYING_TOOL_ARG_KEYS = (
    "file_path",
    "path",
    "command",
    "pattern",
    "url",
    "query",
    "agent_id",
    "title",
)


def format_events_for_compaction(events: list[Any]) -> str:
    """Render a list of session events as a compact text block for the LLM."""
    lines: list[str] = []
    for event in events:
        etype = event.type
        data = event.data

        if etype in ("user_message", "assistant_message"):
            attachments = [a for a in data.get("attachments", []) if isinstance(a, dict)]
            raw_content = merge_content_and_attachment_note(
                str(data.get("content", "")),
                attachments,
            )
            if etype == "user_message":
                payload = render_user_event_content(
                    event,
                    content_override=raw_content,
                    max_content_chars=_MESSAGE_MAX_CHARS,
                )
            else:
                payload = raw_content
                payload = _truncate_middle(payload, _MESSAGE_MAX_CHARS)
        elif etype == "tool_call":
            name = data.get("name", "unknown")
            args = data.get("arguments", "")
            metadata = _tool_event_metadata(data)
            args_text = _format_tool_call_arguments(args)
            payload = f"{name}{metadata} {args_text}".rstrip()
        elif etype == "tool_result":
            name = data.get("name", "")
            result = data.get("result") or data.get("output", "")
            is_error = data.get("is_error", False)
            prefix = f"[ERROR] {name}" if is_error else str(name)
            recovery_hint = tool_result_recovery_hint(data)
            result_text = _truncate(
                _stringify(result),
                _TOOL_RESULT_MAX_CHARS,
                recovery_hint=recovery_hint,
            )
            payload = f"{prefix}{_tool_event_metadata(data)}: {result_text}"
        elif etype == "delegation":
            status = data.get("status", "")
            mode = data.get("mode", "")
            summary = data.get("result_summary", "")
            payload = f"[{mode}/{status}] {summary}"
        else:
            content = data.get("content")
            payload = content if isinstance(content, str) else str(data)

        lines.append(f"[{event.seq}] {etype}: {payload}")
    return "\n".join(lines)


def _stringify(value: Any) -> str:
    """Return a stable text representation for compaction prompts."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except TypeError:
        return str(value)


def _parse_json_object(value: str) -> dict[str, Any] | None:
    stripped = value.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _format_tool_call_arguments(arguments: Any) -> str:
    """Render tool arguments with identifying keys preserved before truncation."""

    args_obj: dict[str, Any] | None
    if isinstance(arguments, dict):
        args_obj = dict(arguments)
    elif isinstance(arguments, str):
        args_obj = _parse_json_object(arguments)
    else:
        args_obj = None

    if args_obj is None:
        return f"args={_truncate(_stringify(arguments), _TOOL_CALL_REMAINDER_MAX_CHARS)}"

    rest = dict(args_obj)
    identifying_parts: list[str] = []
    for key in _IDENTIFYING_TOOL_ARG_KEYS:
        if key not in rest:
            continue
        value = rest.pop(key)
        identifying_parts.append(f"{key}={_stringify_identifier_value(value)}")

    rest_text = _truncate(_stringify(rest), _TOOL_CALL_REMAINDER_MAX_CHARS)
    if identifying_parts:
        return f"{' '.join(identifying_parts)} args={rest_text}"
    return f"args={rest_text}"


def _stringify_identifier_value(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    return repr(_stringify(value))


def _truncate_middle(text: str, max_chars: int) -> str:
    """Cap long message payloads while retaining both the opening and ending."""

    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    marker = f"\n[truncated for compaction: omitted {omitted:,} chars from middle]\n"
    if max_chars <= len(marker):
        return text[:max_chars]
    remaining_chars = max_chars - len(marker)
    head_chars = remaining_chars // 2
    tail_chars = remaining_chars - head_chars
    return text[:head_chars].rstrip() + marker + text[-tail_chars:].lstrip()


def _truncate(
    text: str,
    max_chars: int,
    *,
    recovery_hint: str | None = None,
) -> str:
    """Truncate text for compaction while preserving recovery instructions."""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    marker = f"[truncated for compaction: omitted {omitted:,} chars"
    if recovery_hint:
        marker += f"; {recovery_hint}"
    marker += "]"
    return text[:max_chars].rstrip() + "\n" + marker


def _tool_event_metadata(data: dict[str, Any]) -> str:
    """Return compact tool metadata that helps summaries retain recovery handles."""
    fields: list[str] = []
    for key in ("call_id", "recovery_call_id", "source_call_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            fields.append(f"{key}={value!r}")
    output_size = data.get("output_size")
    if isinstance(output_size, int) and output_size > 0:
        fields.append(f"output_size={output_size}")
    if data.get("has_full_output") is True:
        fields.append("has_full_output=true")
    if data.get("agent_visible") is True:
        fields.append("agent_visible=true")
    if data.get("agent_visible_truncated") is True:
        fields.append("agent_visible_truncated=true")
    if data.get("producer_truncated") is True:
        fields.append("producer_truncated=true")
    artifact_id = data.get("tool_output_artifact_id")
    if isinstance(artifact_id, str) and artifact_id.strip():
        fields.append(f"tool_output_artifact_id={artifact_id!r}")
    return "" if not fields else " " + " ".join(fields)


def tool_result_recovery_hint(data: dict[str, Any]) -> str | None:
    """Return concrete recovery calls for a saved tool result, if available."""
    recovery_call_id = data.get("recovery_call_id")
    call_id = recovery_call_id
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = data.get("call_id") if data.get("has_full_output") is True else None
    if not isinstance(call_id, str) or not call_id.strip():
        producer_call_id = data.get("call_id")
        if data.get("producer_truncated") is True and isinstance(producer_call_id, str):
            return (
                f"producer returned a truncated preview for call_id={producer_call_id!r}; "
                "no controller recovery handle is available"
            )
        return None
    quoted = repr(call_id)
    return (
        f"recover with read_tool_output(call_id={quoted}) or "
        f"search_tool_output(call_id={quoted}, pattern='keyword')"
    )
