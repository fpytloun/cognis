"""Middle-truncation utility for tool outputs.

Preserves the head and tail of large text, removing the middle.
This keeps headers/initial output and final results/exit codes —
both more useful than arbitrary middle content.
"""

from __future__ import annotations

# Minimum size below which middle-truncation is pointless — just
# return the full text.  The marker itself takes ~120-200 chars.
_MIN_TRUNCATION_SIZE = 500


def middle_truncate(
    text: str,
    max_chars: int,
    *,
    call_id: str | None = None,
    head_ratio: float = 0.5,
) -> tuple[str, bool]:
    """Truncate *text* preserving head and tail, removing the middle.

    Returns ``(truncated_text, was_truncated)``.

    *head_ratio* controls the split between head and tail (default 50/50).
    If *call_id* is provided, the truncation marker tells the LLM how to
    recover the full output via ``read_tool_output``.
    """

    if len(text) <= max_chars or max_chars < _MIN_TRUNCATION_SIZE:
        return text, False

    # Reserve space for the marker line
    marker = _build_marker(len(text), call_id)
    available = max_chars - len(marker)
    if available < 100:
        # Not enough room for meaningful head+tail — fall back to head-only
        return text[:max_chars], True

    head_size = int(available * head_ratio)
    tail_size = available - head_size

    return text[:head_size] + marker + text[-tail_size:], True


def _build_marker(total_chars: int, call_id: str | None) -> str:
    parts = [f"\n\n... [middle truncated: {total_chars:,} chars total"]
    if call_id:
        parts.append(
            ", saved output is incomplete here; use "
            f"search_tool_output(call_id='{call_id}', pattern='error|timeout|keyword') to find specific details "
            f"or read_tool_output(call_id='{call_id}') to inspect sequentially"
        )
    parts.append("] ...\n\n")
    return "".join(parts)
