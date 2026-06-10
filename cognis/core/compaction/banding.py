"""Three-band token-budgeted compaction input assembly.

Replaces the old tail-truncation approach with a head/middle-drop/tail
strategy that preserves both the original goal (head) and the most recent
decisions (tail) while dropping the least-signal middle band.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cognis.core.compaction.input_format import format_events_for_compaction

# Band size ratios (must sum to ≤ 1.0; remainder is headroom for wrappers).
_HEAD_RATIO = 0.20  # oldest events — captures original goal / task framing
_TAIL_RATIO = 0.60  # newest events — highest signal for resumption
# 20% headroom for previous-summary wrapper, recovery-handle trailer, etc.

# Chars-per-token estimate used for fast initial sizing before exact counting.
_CHARS_PER_TOKEN_ESTIMATE = 3.5

# Fallback budget when model info is unavailable (chars, not tokens).
_DEFAULT_BUDGET_CHARS = 240_000


@dataclass
class CompactionInput:
    """Result of three-band assembly."""

    text: str
    head_event_count: int
    tail_event_count: int
    dropped_event_count: int
    dropped_seq_start: int | None
    dropped_seq_end: int | None
    estimated_chars: int


def build_compaction_input(
    older_events: list[Any],
    *,
    max_input_tokens: int | None = None,
    count_tokens_fn: Any = None,
    model: str | None = None,
) -> CompactionInput:
    """Assemble the compaction prompt input using a three-band strategy.

    Args:
        older_events: Events to be compacted (the "older" split from _split_events).
        max_input_tokens: Token budget for the assembled text.  When ``None``
            the legacy char-based fallback is used.
        count_tokens_fn: Callable ``(text, model) -> int`` for exact token
            counting.  Used for a final over-budget check after fast assembly.
        model: Model name passed to ``count_tokens_fn``.

    Returns:
        ``CompactionInput`` with the assembled text and band metadata.
    """
    if not older_events:
        return CompactionInput(
            text="",
            head_event_count=0,
            tail_event_count=0,
            dropped_event_count=0,
            dropped_seq_start=None,
            dropped_seq_end=None,
            estimated_chars=0,
        )

    # Determine effective char budget from token budget or fallback.
    if max_input_tokens is not None and max_input_tokens > 0:
        budget_chars = int(max_input_tokens * _CHARS_PER_TOKEN_ESTIMATE)
    else:
        budget_chars = _DEFAULT_BUDGET_CHARS

    head_budget = int(budget_chars * _HEAD_RATIO)
    tail_budget = int(budget_chars * _TAIL_RATIO)

    # Fast path: if everything fits, return as-is (no band drop needed).
    full_text = format_events_for_compaction(older_events)
    if len(full_text) <= budget_chars:
        return CompactionInput(
            text=full_text,
            head_event_count=len(older_events),
            tail_event_count=0,
            dropped_event_count=0,
            dropped_seq_start=None,
            dropped_seq_end=None,
            estimated_chars=len(full_text),
        )

    # Build head band: take events from the start until head_budget is reached.
    head_events: list[Any] = []
    head_chars = 0
    for event in older_events:
        line = format_events_for_compaction([event])
        if head_chars + len(line) > head_budget and head_events:
            break
        head_events.append(event)
        head_chars += len(line) + 1  # +1 for newline

    # Build tail band: take events from the end until tail_budget is reached.
    tail_events: list[Any] = []
    tail_chars = 0
    for event in reversed(older_events):
        if event in head_events:
            break
        line = format_events_for_compaction([event])
        if tail_chars + len(line) > tail_budget and tail_events:
            break
        tail_events.insert(0, event)
        tail_chars += len(line) + 1

    # Determine the dropped middle band.
    head_seqs = {id(e) for e in head_events}
    tail_seqs = {id(e) for e in tail_events}
    dropped_events = [e for e in older_events if id(e) not in head_seqs and id(e) not in tail_seqs]

    dropped_seq_start: int | None = None
    dropped_seq_end: int | None = None
    if dropped_events:
        dropped_seq_start = getattr(dropped_events[0], "seq", None)
        dropped_seq_end = getattr(dropped_events[-1], "seq", None)

    # Assemble the final text.
    parts: list[str] = []
    if head_events:
        parts.append(format_events_for_compaction(head_events))
    if dropped_events:
        n = len(dropped_events)
        seq_range = (
            f"seq {dropped_seq_start}–{dropped_seq_end}"
            if dropped_seq_start is not None
            else f"{n} events"
        )
        parts.append(
            f"[compaction band: omitted {n} events between {seq_range}; "
            "tool outputs from this range remain recoverable by call_id "
            "when recovery handles are present]"
        )
    if tail_events:
        parts.append(format_events_for_compaction(tail_events))

    text = "\n".join(parts)

    # Final exact-token check: if still over budget, widen the drop band
    # by trimming from the tail of the head band and the head of the tail band.
    if count_tokens_fn is not None and model is not None and max_input_tokens is not None:
        try:
            actual_tokens = count_tokens_fn(text, model)
            while actual_tokens > max_input_tokens and (
                len(head_events) > 1 or len(tail_events) > 1
            ):
                # Trim one event from whichever band is larger.
                if len(head_events) >= len(tail_events) and len(head_events) > 1:
                    dropped_events.insert(0, head_events.pop())
                elif len(tail_events) > 1:
                    dropped_events.append(tail_events.pop(0))
                else:
                    break
                # Rebuild.
                if dropped_events:
                    dropped_seq_start = getattr(dropped_events[0], "seq", None)
                    dropped_seq_end = getattr(dropped_events[-1], "seq", None)
                parts = []
                if head_events:
                    parts.append(format_events_for_compaction(head_events))
                if dropped_events:
                    n = len(dropped_events)
                    seq_range = (
                        f"seq {dropped_seq_start}–{dropped_seq_end}"
                        if dropped_seq_start is not None
                        else f"{n} events"
                    )
                    parts.append(
                        f"[compaction band: omitted {n} events between {seq_range}; "
                        "tool outputs from this range remain recoverable by call_id "
                        "when recovery handles are present]"
                    )
                if tail_events:
                    parts.append(format_events_for_compaction(tail_events))
                text = "\n".join(parts)
                actual_tokens = count_tokens_fn(text, model)
        except Exception:
            pass  # Exact counting failed; use the char-estimated result.

    return CompactionInput(
        text=text,
        head_event_count=len(head_events),
        tail_event_count=len(tail_events),
        dropped_event_count=len(dropped_events),
        dropped_seq_start=dropped_seq_start,
        dropped_seq_end=dropped_seq_end,
        estimated_chars=len(text),
    )
