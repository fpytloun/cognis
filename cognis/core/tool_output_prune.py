"""Post-step pruning of in-context tool outputs.

After an agent loop completes a step, very long-running steps can leave
50+ tool-result events in the recent history. Each subsequent context
assembly within the same conversation has to ferry that growing tail
into every LLM call. This module mirrors OpenCode's ``prune`` logic:
walk back from the most recent events, keep the last few user turns
intact, accumulate tool-result tokens, and once the tail exceeds
``PRUNE_PROTECT`` tokens mark older tool result call ids as pruned.

Pruning is purely a controller-side context-assembly view. The Intaris
event store is unchanged. The tool output store also retains the
original payload, so the model can always recover via ``read_tool_output``
or ``read_tool_output_anchor`` — Cognis ends up strictly better than
OpenCode here, where pruned content is unrecoverable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Token thresholds match OpenCode's ``compaction.ts`` constants so the
# behaviour is comparable across the two harnesses.
PRUNE_PROTECT = 40_000
PRUNE_MIN_USER_TURNS = 2

# Tool names whose results are structurally important to keep verbatim.
# These are typically small, frequently consulted, and form the
# scaffolding of a session — pruning them is never the right call.
PRUNE_PROTECTED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "skill_load",
        "step_complete",
        "step_todo_write",
        "step_todo_list",
        "step_request_questions",
        "write_deliverable",
    }
)


def _default_token_count(text: str) -> int:
    """Cheap fallback token estimator (~4 chars per token)."""

    return max(1, len(text) // 4)


@dataclass(slots=True)
class PruneCandidate:
    """A tool result that may be pruned from the in-context view."""

    call_id: str
    tool_name: str
    output: str
    is_user_turn: bool = False  # Sentinel for user-message events.


def select_prune_call_ids(
    events: Iterable[PruneCandidate],
    *,
    token_counter: Callable[[str], int] | None = None,
    protect_tokens: int = PRUNE_PROTECT,
    min_user_turns: int = PRUNE_MIN_USER_TURNS,
) -> set[str]:
    """Decide which tool-result call ids should be pruned.

    ``events`` is the stream in *forward* (chronological) order. The
    walk is performed in reverse — we keep the latest results untouched
    and prune the older ones once the accumulated token count exceeds
    ``protect_tokens``. The walk stops as soon as the budget is
    exceeded; everything before the cut-off is pruned.
    """

    counter = token_counter or _default_token_count

    items = list(events)
    if not items:
        return set()

    user_turns_seen = 0
    accumulated_tokens = 0
    prune_ids: set[str] = set()

    for item in reversed(items):
        if item.is_user_turn:
            user_turns_seen += 1
            continue
        if not item.call_id or not item.output:
            continue
        if item.tool_name in PRUNE_PROTECTED_TOOL_NAMES:
            continue
        if user_turns_seen < min_user_turns:
            # Stay within the recent turn window untouched, even if it
            # already exceeds the protect budget — we never prune the
            # active conversation context.
            continue
        try:
            cost = counter(item.output)
        except Exception:
            cost = _default_token_count(item.output)
        accumulated_tokens += cost
        if accumulated_tokens <= protect_tokens:
            continue
        # Past the protect window — this event and everything older
        # than it should render as a clearance marker next turn.
        prune_ids.add(item.call_id)

    return prune_ids


def cleared_tool_result_marker(call_id: str) -> str:
    """Return the marker text shown to the model for a pruned tool result."""

    return (
        "[Old tool result content cleared to free context. "
        f"Recover with read_tool_output(call_id='{call_id}') if you need "
        "the original content.]"
    )
