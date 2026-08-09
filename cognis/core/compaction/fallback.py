"""Sliding-window mechanical fallback summary for when LLM compaction fails.

This is a last-resort path.  The header explicitly signals irreversible
information loss so the model (and user) know continuity may be degraded.
"""

from __future__ import annotations

import json
from typing import Any

from cognis.core.attachment_utils import merge_content_and_attachment_note
from cognis.core.compaction.recovery import (
    RECOVERY_USAGE_HINT,
    recoverable_tool_output_lines,
    remove_recoverable_tool_output_sections,
)
from cognis.core.message_envelope import render_user_event_content

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


def build_sliding_window_summary(
    events: list[Any],
    *,
    previous_summary: str | None = None,
) -> str:
    """Build a sliding-window mechanical summary from session events.

    Keeps the last N user messages, assistant finals, and deliverables
    verbatim, followed by a counts trailer and recoverable-handle block.
    """
    type_counts: dict[str, int] = {}
    original_user_messages: list[str] = []
    user_messages: list[str] = []
    assistant_finals: list[str] = []
    deliverables: list[str] = []

    for event in events:
        etype = event.type
        type_counts[etype] = type_counts.get(etype, 0) + 1
        data = event.data

        if etype == "user_message":
            raw_content = merge_content_and_attachment_note(
                str(data.get("content", "")),
                [a for a in data.get("attachments", []) if isinstance(a, dict)],
            )
            user_content = render_user_event_content(
                event,
                content_override=raw_content,
                max_content_chars=_PER_MESSAGE_MAX_CHARS,
            )
            if user_content.strip():
                cleaned = user_content.strip()
                if len(original_user_messages) < 2:
                    original_user_messages.append(cleaned)
                user_messages.append(cleaned)

        elif etype == "assistant_message":
            content = data.get("content")
            # Only keep "final" assistant messages (no trailing tool calls).
            tool_calls = data.get("tool_calls")
            if isinstance(content, str) and content.strip() and not tool_calls:
                assistant_finals.append(content.strip()[:_PER_MESSAGE_MAX_CHARS])

        elif etype == "tool_call":
            # Capture write_deliverable content from the call arguments.  The
            # result event is only a confirmation receipt in the real tool path.
            name = data.get("name", "")
            if name == "write_deliverable":
                deliverable_content = _extract_write_deliverable_content(data.get("arguments"))
                if deliverable_content:
                    deliverables.append(deliverable_content[:_PER_MESSAGE_MAX_CHARS])

    recoverable_lines = recoverable_tool_output_lines(events)

    lines: list[str] = [_FALLBACK_HEADER, ""]

    if previous_summary:
        lines.append("## Previous anchored summary (verbatim):")
        lines.append(remove_recoverable_tool_output_sections(previous_summary))
        lines.append("")

    if original_user_messages:
        lines.append("## Original request (verbatim):")
        for msg in original_user_messages:
            lines.append(f"- {msg}")
        lines.append("")

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
        lines.append("## Recoverable Tool Evidence")
        lines.extend(recoverable_lines)
        lines.append(RECOVERY_USAGE_HINT)

    return "\n".join(lines)


def _extract_write_deliverable_content(arguments: Any) -> str | None:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            arguments = parsed
    if not isinstance(arguments, dict):
        return None
    for key in ("content", "text", "markdown", "body"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
