"""Timeline visibility helpers shared by REST and WebSocket projections."""

from __future__ import annotations

from typing import Any


def _notice_text(data: dict[str, Any]) -> str:
    for key in ("message", "text", "content"):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def is_transient_compaction_start_notice(data: dict[str, Any]) -> bool:
    """Return true for compaction-start notices represented by lifecycle state.

    Compaction has first-class timeline events/cards. The pre-compaction
    explanatory text is transient state and should not also render as a durable
    system message, otherwise old notices can leak into the latest chat window.
    """

    if data.get("kind") == "compaction_start":
        return True

    text = _notice_text(data).lower()
    if text.startswith("automatic compaction is starting before this turn continues"):
        return True

    return (
        text.startswith("the model provider rejected the request because the context window is full")
        and "compacting the saved conversation" in text
        and data.get("status") in {"started", "running"}
    )


def is_visible_persisted_system_message(data: dict[str, Any]) -> bool:
    """Return true for persisted system messages intended for chat timeline UI."""

    if is_transient_compaction_start_notice(data):
        return False

    notice_id = data.get("notice_id")
    if isinstance(notice_id, str) and notice_id:
        return True

    if data.get("kind") == "turn_initiated":
        return True

    return data.get("event") == "turn_initiated"
