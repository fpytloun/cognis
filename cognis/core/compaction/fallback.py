"""Sliding-window mechanical fallback summary for when LLM compaction fails.

This is a last-resort path.  The header explicitly signals irreversible
information loss so the model (and user) know continuity may be degraded.
"""

from __future__ import annotations

from typing import Any

from cognis.core.compaction.recovery import recoverable_tool_output_lines

# How many of each message type to keep verbatim.
_USER_MESSAGE_KEEP = 8
_ASSISTANT_MESSAGE_KEEP = 4
_DELIVERABLE_KEEP = 4

# Maximum chars per verbatim message (prevents one huge message dominating).
_PER_MESSAGE_MAX_CHARS = 800

_FALLBACK_HEADER = (
    "[WARNING] LLM compaction unavailable; this is a mechanical fallback summary.\n"
    "The verbatim messages below are the most recent conversation content.\n"
    "Earlier conversation context has been lost and is only summarized by event counts.\n"
    "This may degrade continuity — consider /new if continuation feels off."
)


def build_sliding_window_summary(events: list[Any]) -> str:
    """Build a sliding-window mechanical summary from session events.

    Keeps the last N user messages, assistant finals, and deliverables
    verbatim, followed by a counts trailer and recoverable-handle block.
    """
    type_counts: dict[str, int] = {}
    user_messages: list[str] = []
    assistant_finals: list[str] = []
    deliverables: list[str] = []

    for event in events:
        etype = event.type
        type_counts[etype] = type_counts.get(etype, 0) + 1
        data = event.data

        if etype == "user_message":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                user_messages.append(content.strip()[:_PER_MESSAGE_MAX_CHARS])

        elif etype == "assistant_message":
            content = data.get("content")
            # Only keep "final" assistant messages (no trailing tool calls).
            tool_calls = data.get("tool_calls")
            if isinstance(content, str) and content.strip() and not tool_calls:
                assistant_finals.append(content.strip()[:_PER_MESSAGE_MAX_CHARS])

        elif etype == "tool_result":
            # Capture write_deliverable outputs as high-signal artifacts.
            name = data.get("name", "")
            if name == "write_deliverable":
                result = data.get("result") or data.get("output", "")
                if isinstance(result, str) and result.strip():
                    deliverables.append(result.strip()[:_PER_MESSAGE_MAX_CHARS])

    recoverable_lines = recoverable_tool_output_lines(events)

    lines: list[str] = [_FALLBACK_HEADER, ""]

    if user_messages:
        lines.append("## Recent user messages (verbatim, most recent last):")
        for msg in user_messages[-_USER_MESSAGE_KEEP:]:
            lines.append(f"- {msg}")
        lines.append("")

    if assistant_finals:
        lines.append("## Recent assistant replies (verbatim, most recent last):")
        for msg in assistant_finals[-_ASSISTANT_MESSAGE_KEEP:]:
            lines.append(f"- {msg}")
        lines.append("")

    if deliverables:
        lines.append("## Recent deliverables (verbatim, most recent last):")
        for msg in deliverables[-_DELIVERABLE_KEEP:]:
            lines.append(f"- {msg}")
        lines.append("")

    lines.append("## Event counts (coarse summary of older history):")
    for etype, count in sorted(type_counts.items()):
        lines.append(f"- {etype}: {count}")

    if recoverable_lines:
        lines.append("")
        lines.append("Recoverable tool outputs before compaction:")
        lines.extend(recoverable_lines)

    return "\n".join(lines)
