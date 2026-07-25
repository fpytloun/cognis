"""Middle-truncation utility for tool outputs.

Preserves the head and tail of large text, removing the middle.
This keeps headers/initial output and final results/exit codes —
both more useful than arbitrary middle content.

When a token counter is available, callers can provide a token budget so the
result stays under an approximate token ceiling instead of only a char ceiling.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

# Minimum size below which middle-truncation is pointless — just
# return the full text.  The marker itself takes ~120-200 chars.
_MIN_TRUNCATION_SIZE = 500

# How many real anchors to enumerate inline in the truncation marker.
# More than this gets noisy and bloats the marker; the model can call
# ``list_tool_output_anchors`` for the full set.
_MAX_INLINE_ANCHORS = 5
_SAFE_ANCHOR_RE = re.compile(r"[^A-Za-z0-9._:-]+")


def middle_truncate(
    text: str,
    max_chars: int,
    *,
    call_id: str | None = None,
    head_ratio: float = 0.5,
    token_counter: Callable[[str], int] | None = None,
    max_tokens: int | None = None,
    anchors: Sequence[str] | None = None,
    anchors_available: bool | None = None,
) -> tuple[str, bool]:
    """Truncate *text* preserving head and tail, removing the middle.

    Returns ``(truncated_text, was_truncated)``.

    *head_ratio* controls the split between head and tail (default 50/50).
    If *call_id* is provided, the truncation marker tells the LLM how to
    recover the full output via ``read_tool_output``.
    *anchors* is an optional sequence of real anchor names discovered for
    this output; up to ``_MAX_INLINE_ANCHORS`` of them are listed inline so
    the model can recall a specific section without first calling
    ``list_tool_output_anchors``.
    """

    if len(text) <= max_chars or max_chars < _MIN_TRUNCATION_SIZE:
        return text, False

    if token_counter is not None and max_tokens is not None and max_tokens > 0:
        try:
            total_tokens = token_counter(text)
        except Exception:
            total_tokens = 0
        if total_tokens > 0 and total_tokens > max_tokens:
            return _middle_truncate_by_tokens(
                text,
                max_chars=max_chars,
                max_tokens=max_tokens,
                token_counter=token_counter,
                call_id=call_id,
                head_ratio=head_ratio,
                anchors=anchors,
                anchors_available=anchors_available,
            )

    return _middle_truncate_chars(
        text,
        max_chars,
        call_id=call_id,
        head_ratio=head_ratio,
        anchors=anchors,
        anchors_available=anchors_available,
    )


def _middle_truncate_chars(
    text: str,
    max_chars: int,
    *,
    call_id: str | None = None,
    head_ratio: float = 0.5,
    anchors: Sequence[str] | None = None,
    anchors_available: bool | None = None,
) -> tuple[str, bool]:
    """Char-budget middle truncation helper."""

    # Reserve space for the marker line
    marker = _build_marker(len(text), call_id, anchors=anchors, anchors_available=anchors_available)
    available = max_chars - len(marker)
    if available < 100:
        # Not enough room for meaningful head+tail — fall back to head-only
        return text[:max_chars], True

    head_size = _snap_head_to_newline(text, int(available * head_ratio))
    tail_size = _snap_tail_to_newline(text, available - head_size)

    return text[:head_size] + marker + text[-tail_size:], True


def _snap_head_to_newline(text: str, size: int) -> int:
    if size <= 0 or size >= len(text):
        return size
    window_start = max(0, size - 2000)
    newline = text.rfind("\n", window_start, size)
    return newline + 1 if newline >= 0 else size


def _snap_tail_to_newline(text: str, size: int) -> int:
    if size <= 0 or size >= len(text):
        return size
    start = len(text) - size
    window_end = min(len(text), start + 2000)
    newline = text.find("\n", start, window_end)
    return len(text) - newline - 1 if newline >= 0 else size


def _middle_truncate_by_tokens(
    text: str,
    *,
    max_chars: int,
    max_tokens: int,
    token_counter: Callable[[str], int],
    call_id: str | None,
    head_ratio: float,
    anchors: Sequence[str] | None = None,
    anchors_available: bool | None = None,
) -> tuple[str, bool]:
    """Approximate token-budget truncation using iterative char scaling."""

    try:
        total_tokens = max(1, token_counter(text))
    except Exception:
        return _middle_truncate_chars(
            text,
            max_chars,
            call_id=call_id,
            head_ratio=head_ratio,
            anchors=anchors,
            anchors_available=anchors_available,
        )

    scaled_chars = int(len(text) * (max_tokens / total_tokens))
    candidate_chars = min(max_chars, max(_MIN_TRUNCATION_SIZE, scaled_chars))
    truncated, _ = _middle_truncate_chars(
        text,
        candidate_chars,
        call_id=call_id,
        head_ratio=head_ratio,
        anchors=anchors,
        anchors_available=anchors_available,
    )

    for _ in range(4):
        try:
            current_tokens = token_counter(truncated)
        except Exception:
            break
        if current_tokens <= max_tokens:
            return truncated, True
        scale = max_tokens / max(1, current_tokens)
        candidate_chars = max(_MIN_TRUNCATION_SIZE, int(candidate_chars * scale * 0.95))
        truncated, _ = _middle_truncate_chars(
            text,
            candidate_chars,
            call_id=call_id,
            head_ratio=head_ratio,
            anchors=anchors,
            anchors_available=anchors_available,
        )

    return truncated, True


def _build_marker(
    total_chars: int,
    call_id: str | None,
    *,
    anchors: Sequence[str] | None = None,
    anchors_available: bool | None = None,
) -> str:
    parts = [f"\n\n... [middle truncated: {total_chars:,} chars total"]
    if call_id:
        anchor_names: list[str] = []
        seen: set[str] = set()
        for value in anchors or []:
            if not isinstance(value, str):
                continue
            name = _SAFE_ANCHOR_RE.sub("-", value.strip()).strip("-")[:120].rstrip("-")
            if name and name not in seen:
                anchor_names.append(name)
                seen.add(name)
        has_anchors = bool(anchor_names)
        recovery_calls = []
        if has_anchors:
            recovery_calls.append(f"list_tool_output_anchors(call_id='{call_id}')")
        if anchor_names:
            recovery_calls.append(
                f"read_tool_output_anchor(call_id='{call_id}', anchor='{anchor_names[0]}')"
            )
        recovery_calls.extend(
            [
                f"search_tool_output(call_id='{call_id}', pattern='error|timeout|keyword')",
                f"read_tool_output(call_id='{call_id}')",
            ]
        )
        parts.append(", use " + ", ".join(recovery_calls))
        if anchor_names:
            preview = anchor_names[:_MAX_INLINE_ANCHORS]
            suffix = (
                ""
                if len(anchor_names) <= _MAX_INLINE_ANCHORS
                else f", +{len(anchor_names) - _MAX_INLINE_ANCHORS} more"
            )
            parts.append(f". Available anchors: {', '.join(preview)}{suffix}")
    parts.append("] ...\n\n")
    return "".join(parts)
