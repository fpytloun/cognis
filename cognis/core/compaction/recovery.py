"""Recovery-handle extraction and injection for compaction summaries."""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter

from cognis.core.compaction.input_format import tool_result_recovery_hint

COMPACTION_HANDLES_CAPPED = Counter(
    "cognis_compaction_recoverable_handles_capped_total",
    "Times the recoverable-handles block was capped to max_entries",
)

# Maximum number of recoverable-handle entries appended to a summary.
# Entries are ranked by output_size desc so the largest (most valuable) survive.
_MAX_RECOVERABLE_HANDLES = 50


def recoverable_tool_output_lines(
    events: list[Any], *, max_entries: int = _MAX_RECOVERABLE_HANDLES
) -> list[str]:
    """Return deterministic recoverable tool-output handle lines, capped and ranked."""
    candidates: list[tuple[int, str, str]] = []  # (output_size, name, hint_line)
    for event in events:
        if event.type != "tool_result":
            continue
        hint = tool_result_recovery_hint(event.data)
        if not hint:
            continue
        name = event.data.get("name") or "tool"
        output_size = event.data.get("output_size") or 0
        candidates.append((output_size, name, f"- [{event.seq}] {name}: {hint}"))

    # Rank by output_size descending so the most valuable handles survive the cap.
    candidates.sort(key=lambda c: c[0], reverse=True)

    lines = [c[2] for c in candidates[:max_entries]]
    if len(candidates) > max_entries:
        COMPACTION_HANDLES_CAPPED.inc()
        lines.append(
            f"[{len(candidates) - max_entries} more recoverable outputs not shown; "
            "use list_tool_outputs to enumerate]"
        )
    return lines


def append_recoverable_tool_output_handles(
    summary: str,
    events: list[Any],
    *,
    max_entries: int = _MAX_RECOVERABLE_HANDLES,
) -> str:
    """Ensure LLM compaction cannot drop saved tool-output recovery handles."""
    lines = recoverable_tool_output_lines(events, max_entries=max_entries)
    if not lines:
        return summary
    block_lines = ["Recoverable tool outputs before compaction:"]
    block_lines.extend(lines)
    block = "\n".join(block_lines)
    if block in summary:
        return summary
    return summary.rstrip() + "\n\n" + block
