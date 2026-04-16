"""Per-turn pruning of old tool outputs from the LLM context.

After each agent turn, walk backwards through tool result messages and
replace old outputs beyond a token protection window with a cleared
marker.  The cleared marker includes the ``call_id`` so the LLM can
recover the full output via ``read_tool_output``.

This is a *view-layer* operation — it only affects what the LLM sees,
not what Intaris stores.  Intaris always keeps the middle-truncated
preview, and the ToolOutputStore keeps the full executor output on
disk.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

PRUNE_PROTECT_TOKENS = 40_000
PRUNE_MINIMUM_TOKENS = 20_000
_ARG_CLEAR_THRESHOLD = 1_000  # clear tool call arguments above this size


def _default_token_estimate(text: str) -> int:
    """Cheap heuristic: 1 token ≈ 4 chars."""
    return max(1, len(text) // 4)


def prune_tool_outputs(
    messages: list[dict[str, Any]],
    *,
    protect_tokens: int = PRUNE_PROTECT_TOKENS,
    minimum_savings: int = PRUNE_MINIMUM_TOKENS,
    arg_clear_threshold: int = _ARG_CLEAR_THRESHOLD,
    token_counter: Callable[[str], int] | None = None,
) -> list[dict[str, Any]]:
    """Replace old tool outputs with cleared markers.

    Walks backwards through *messages*.  Protects the most recent
    *protect_tokens* worth of tool result content.  Replaces older
    tool results with a cleared message that includes the ``call_id``
    for recovery via ``read_tool_output``.

    Tool call arguments (``role="assistant"`` with ``tool_calls``) are
    also cleared when their serialized size exceeds *arg_clear_threshold*.

    Returns a **new** message list (does not mutate input).
    """
    count = token_counter or _default_token_estimate
    result = list(messages)

    # Phase 1: Walk backwards to identify pruneable tool results.
    protected_tokens = 0
    pruneable_result_indices: list[int] = []
    pruneable_call_ids: set[str] = set()  # call_ids of pruned results

    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        tokens = count(content) if isinstance(content, str) else 0
        if protected_tokens + tokens <= protect_tokens:
            protected_tokens += tokens
        else:
            pruneable_result_indices.append(i)
            call_id = msg.get("tool_call_id")
            if call_id:
                pruneable_call_ids.add(call_id)

    # Phase 1b: Identify assistant messages with large tool_calls
    # whose corresponding results are being pruned.
    pruneable_call_indices: list[int] = []
    for i, msg in enumerate(result):
        if msg.get("role") != "assistant" or not isinstance(msg.get("tool_calls"), list):
            continue
        tool_calls = msg["tool_calls"]
        # Check if any tool_call in this message has a pruned result
        has_pruned = any(
            isinstance(tc, dict) and tc.get("id") in pruneable_call_ids for tc in tool_calls
        )
        if not has_pruned:
            continue
        total_arg_size = sum(
            len(json.dumps(tc.get("function", {}).get("arguments", {}), default=str))
            for tc in tool_calls
            if isinstance(tc, dict)
        )
        if total_arg_size > arg_clear_threshold:
            pruneable_call_indices.append(i)

    # Phase 2: Calculate total savings
    total_savings = 0
    for i in pruneable_result_indices:
        content = result[i].get("content", "")
        total_savings += count(content) if isinstance(content, str) else 0
    for i in pruneable_call_indices:
        for tc in result[i].get("tool_calls", []):
            if isinstance(tc, dict):
                args = tc.get("function", {}).get("arguments", {})
                total_savings += count(json.dumps(args, default=str))

    if total_savings < minimum_savings:
        return result

    # Phase 3: Apply pruning
    for i in pruneable_result_indices:
        msg = result[i]
        call_id = msg.get("tool_call_id", "unknown")
        result[i] = {
            **msg,
            "content": (
                "[Tool result cleared from context; content is incomplete here. "
                f"Use list_tool_output_anchors(call_id='{call_id}') for structured sections, "
                f"read_tool_output_anchor(call_id='{call_id}', anchor='result:1') for one anchored section, "
                f"Use search_tool_output(call_id='{call_id}', pattern='error|timeout|keyword') for targeted lookup "
                f"or read_tool_output(call_id='{call_id}') to inspect the saved output.]"
            ),
        }

    for i in pruneable_call_indices:
        msg = result[i]
        cleared_calls = []
        for tc in msg.get("tool_calls", []):
            if not isinstance(tc, dict):
                cleared_calls.append(tc)
                continue
            func = tc.get("function", {})
            raw_args = func.get("arguments", {})
            args_str = (
                json.dumps(raw_args, default=str) if not isinstance(raw_args, str) else raw_args
            )
            if len(args_str) > arg_clear_threshold:
                cleared_func = {
                    **func,
                    "arguments": json.dumps(
                        {"_cleared": f"[Arguments cleared — {len(args_str)} chars]"}
                    ),
                }
                cleared_calls.append({**tc, "function": cleared_func})
            else:
                cleared_calls.append(tc)
        result[i] = {**msg, "tool_calls": cleared_calls}

    return result
