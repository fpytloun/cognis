"""Model-facing projection of rich session history.

This module keeps the durable transcript rich for audit/UI while projecting
older tool groups into deterministic compact placeholders before they are sent
back to the model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_PRESERVED_TOOL_GROUPS = 10
DEFAULT_PRESERVED_TOOL_BYTES = 200_000
# Backward-compatible alias used by existing call sites.
DEFAULT_COMPACTED_TOOL_GROUPS = DEFAULT_PRESERVED_TOOL_GROUPS
_ARG_CLEAR_THRESHOLD = 1_000


def default_token_estimate(text: str) -> int:
    """Cheap heuristic: 1 token ~= 4 chars."""

    return max(1, len(text) // 4)


def build_compacted_tool_result_placeholder(message: dict[str, Any]) -> str:
    """Build a deterministic placeholder for a compacted tool result."""

    tool_name = str(message.get("_tool_name") or "tool")
    recovery_call_id = message.get("_recovery_call_id")
    if not isinstance(recovery_call_id, str) or not recovery_call_id.strip():
        recovery_call_id = None
    original_call_id = str(message.get("tool_call_id") or "unknown")
    source_call_id = message.get("_source_call_id")
    if not isinstance(source_call_id, str) or not source_call_id.strip():
        source_call_id = None
    output_size = message.get("_output_size")
    size_note = (
        f" Original preview size: {int(output_size):,} chars."
        if isinstance(output_size, int) and output_size > 0
        else ""
    )
    source_note = ""
    if source_call_id is not None and source_call_id not in {original_call_id, recovery_call_id}:
        source_note = f" This helper output was derived from source call_id '{source_call_id}'."
    if recovery_call_id is None:
        return (
            "[Tool output omitted from prompt. "
            f"Tool: {tool_name}. Original call_id: {original_call_id}.{size_note}{source_note} "
            "No saved output handle is available.]"
        )
    return (
        "[Tool output omitted from prompt. "
        f"Tool: {tool_name}. Original call_id: {original_call_id}.{size_note}{source_note} "
        f"Recover with call_id '{recovery_call_id}' only if a specific missing detail is needed.]"
    )


def build_compacted_tool_attachment_placeholder(message: dict[str, Any]) -> str:
    """Build a deterministic placeholder for compacted tool attachment context."""

    recovery_call_id = message.get("_recovery_call_id") or message.get("_tool_call_id")
    if isinstance(recovery_call_id, str) and recovery_call_id.strip():
        return (
            "[Tool attachment context cleared from view. Tool attachment context compacted from prompt. "
            f"Use read_tool_output(call_id='{recovery_call_id}') or the UI attachment viewer "
            "if the missing attachment content matters.]"
        )
    return "[Tool attachment context cleared from view. Tool attachment context compacted from prompt.]"


def clear_large_tool_call_arguments(
    message: dict[str, Any],
    *,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
) -> dict[str, Any]:
    """Clear oversized assistant tool-call arguments in a projected message."""

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return dict(message)
    cleared_calls: list[Any] = []
    changed = False
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            cleared_calls.append(tool_call)
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            cleared_calls.append(tool_call)
            continue
        raw_args = function.get("arguments", {})
        args_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, default=str)
        if len(args_str) <= arg_clear_threshold:
            cleared_calls.append(tool_call)
            continue
        changed = True
        cleared_calls.append(
            {
                **tool_call,
                "function": {
                    **function,
                    "arguments": json.dumps(
                        {"_cleared": f"[Arguments cleared - {len(args_str)} chars]"}
                    ),
                },
            }
        )
    if not changed:
        return dict(message)
    return {**message, "tool_calls": cleared_calls}


@dataclass(frozen=True)
class ProjectionResult:
    """Projected model transcript and its mutable tail boundary."""

    messages: list[dict[str, Any]]
    mutable_start_index: int


@dataclass(frozen=True)
class _ToolGroup:
    assistant_index: int
    message_indices: tuple[int, ...]
    call_ids: frozenset[str]
    completed: bool
    protected: bool


def project_messages(
    messages: list[dict[str, Any]],
    *,
    preserve_recent_completed_tool_groups: int = DEFAULT_PRESERVED_TOOL_GROUPS,
    preserve_recent_completed_tool_bytes: int = DEFAULT_PRESERVED_TOOL_BYTES,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
) -> ProjectionResult:
    """Project a rich transcript into a compact model-facing view."""

    groups = _collect_tool_groups(messages)
    if not groups:
        return ProjectionResult(messages=list(messages), mutable_start_index=0)

    preserve_recent_completed_tool_groups = max(0, int(preserve_recent_completed_tool_groups))
    latest_turn_start = _latest_real_user_index(messages)
    completed_groups = [
        group
        for group in groups
        if group.completed and not group.protected and group.assistant_index < latest_turn_start
    ]
    preserved_slice = (
        completed_groups[-preserve_recent_completed_tool_groups:]
        if preserve_recent_completed_tool_groups > 0
        else []
    )
    preserve_recent_completed_tool_bytes = max(0, int(preserve_recent_completed_tool_bytes))
    while (
        preserve_recent_completed_tool_bytes > 0
        and len(preserved_slice) > 1
        and sum(_estimated_group_bytes(messages, group) for group in preserved_slice)
        > preserve_recent_completed_tool_bytes
    ):
        preserved_slice = preserved_slice[1:]
    preserved_assistant_indices = {group.assistant_index for group in preserved_slice}
    compacted_groups = [
        group
        for group in completed_groups
        if group.assistant_index not in preserved_assistant_indices
    ]
    if not compacted_groups:
        return ProjectionResult(messages=list(messages), mutable_start_index=0)

    compacted_assistant_indices = {group.assistant_index for group in compacted_groups}
    compacted_call_ids = {call_id for group in compacted_groups for call_id in group.call_ids}

    projected: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index in compacted_assistant_indices:
            projected.append(
                clear_large_tool_call_arguments(message, arg_clear_threshold=arg_clear_threshold)
            )
            continue
        if message.get("role") == "tool" and message.get("tool_call_id") in compacted_call_ids:
            projected.append(
                {
                    **message,
                    "content": build_compacted_tool_result_placeholder(message),
                    "_projected_compacted": True,
                }
            )
            continue
        if message.get("_tool_attachment_context") and (
            message.get("_tool_call_id") in compacted_call_ids
            or message.get("tool_call_id") in compacted_call_ids
        ):
            projected.append(
                {
                    **message,
                    "role": "system",
                    "content": build_compacted_tool_attachment_placeholder(message),
                    "_projected_compacted": True,
                }
            )
            continue
        projected.append(dict(message))

    mutable_start_index = min(
        (
            group.assistant_index
            for group in groups
            if group.assistant_index not in compacted_assistant_indices
        ),
        default=len(projected),
    )
    if mutable_start_index >= len(projected):
        mutable_start_index = len(projected)
    return ProjectionResult(messages=projected, mutable_start_index=mutable_start_index)


def _estimated_group_bytes(messages: list[dict[str, Any]], group: _ToolGroup) -> int:
    total = 0
    for index in group.message_indices:
        if index < 0 or index >= len(messages):
            continue
        total += len(json.dumps(messages[index], default=str))
    return total


def prune_projected_messages(
    messages: list[dict[str, Any]],
    *,
    protect_tokens: int,
    minimum_savings: int,
    min_index_to_modify: int = 0,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
    token_counter: Callable[[str], int] | None = None,
) -> list[dict[str, Any]]:
    """Fallback pruning for the mutable tail after normal projection."""

    count = token_counter or default_token_estimate
    result = list(messages)
    latest_turn_start = _latest_real_user_index(result)
    protected_tokens = 0
    pruneable_result_indices: list[int] = []
    pruneable_call_ids: set[str] = set()
    pruneable_attachment_indices: list[int] = []

    for index in range(len(result) - 1, min_index_to_modify - 1, -1):
        if index >= latest_turn_start:
            continue
        message = result[index]
        if message.get("role") != "tool":
            continue
        if message.get("_protected_tool_output") or message.get("_projected_compacted"):
            continue
        content = message.get("content", "")
        tokens = count(content) if isinstance(content, str) else 0
        if protected_tokens + tokens <= protect_tokens:
            protected_tokens += tokens
        else:
            pruneable_result_indices.append(index)
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                pruneable_call_ids.add(call_id)

    pruneable_call_indices: list[int] = []
    for index in range(min_index_to_modify, latest_turn_start):
        message = result[index]
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        tool_calls = message["tool_calls"]
        if not any(
            isinstance(tc, dict) and tc.get("id") in pruneable_call_ids for tc in tool_calls
        ):
            continue
        total_arg_size = sum(
            len(
                json.dumps(
                    tc.get("function", {}).get("arguments", {}),
                    default=str,
                )
            )
            for tc in tool_calls
            if isinstance(tc, dict)
        )
        if total_arg_size > arg_clear_threshold:
            pruneable_call_indices.append(index)

    for index in range(min_index_to_modify, latest_turn_start):
        message = result[index]
        if not message.get("_tool_attachment_context") or message.get("_projected_compacted"):
            continue
        call_id = message.get("_tool_call_id") or message.get("tool_call_id")
        if call_id in pruneable_call_ids:
            pruneable_attachment_indices.append(index)

    total_savings = 0
    for index in pruneable_result_indices:
        content = result[index].get("content", "")
        total_savings += count(content) if isinstance(content, str) else 0
    for index in pruneable_call_indices:
        for tool_call in result[index].get("tool_calls", []):
            if isinstance(tool_call, dict):
                args = tool_call.get("function", {}).get("arguments", {})
                total_savings += count(json.dumps(args, default=str))
    for index in pruneable_attachment_indices:
        total_savings += count(json.dumps(result[index].get("content", ""), default=str))
    if total_savings < minimum_savings:
        return result

    for index in pruneable_result_indices:
        message = result[index]
        result[index] = {
            **message,
            "content": build_compacted_tool_result_placeholder(message),
            "_projected_compacted": True,
        }
    for index in pruneable_call_indices:
        result[index] = clear_large_tool_call_arguments(
            result[index], arg_clear_threshold=arg_clear_threshold
        )
    for index in pruneable_attachment_indices:
        message = result[index]
        result[index] = {
            **message,
            "role": "system",
            "content": build_compacted_tool_attachment_placeholder(message),
            "_projected_compacted": True,
        }
    return result


def _collect_tool_groups(messages: list[dict[str, Any]]) -> list[_ToolGroup]:
    groups: list[_ToolGroup] = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
            continue
        call_ids = {
            str(tool_call.get("id") or tool_call.get("call_id") or "")
            for tool_call in message["tool_calls"]
            if isinstance(tool_call, dict)
        } - {""}
        if not call_ids:
            continue
        message_indices = [index]
        seen_results: set[str] = set()
        protected = False
        for follow_index in range(index + 1, len(messages)):
            follow = messages[follow_index]
            if follow.get("role") == "tool" and follow.get("tool_call_id") in call_ids:
                message_indices.append(follow_index)
                seen_results.add(str(follow.get("tool_call_id")))
                if follow.get("_protected_tool_output"):
                    protected = True
                continue
            if follow.get("_tool_attachment_context") and (
                follow.get("_tool_call_id") in call_ids or follow.get("tool_call_id") in call_ids
            ):
                message_indices.append(follow_index)
                continue
            break
        groups.append(
            _ToolGroup(
                assistant_index=index,
                message_indices=tuple(message_indices),
                call_ids=frozenset(call_ids),
                completed=seen_results == call_ids,
                protected=protected,
            )
        )
    return groups


def _latest_real_user_index(messages: list[dict[str, Any]]) -> int:
    """Return the start index of the latest real user turn.

    Tool attachment contexts can be projected as user messages so providers can
    see media produced by tools. They are not human/workflow turn boundaries and
    must not cause earlier same-turn tool evidence to become pruneable.
    """

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_real_turn_boundary(message):
            return index
    return len(messages)


def _is_real_turn_boundary(message: dict[str, Any]) -> bool:
    role = message.get("role")
    if role == "user":
        return not bool(message.get("_tool_attachment_context"))
    if role != "system":
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    return content.lstrip().startswith("## Workflow Task")
