"""Helpers for sticky web-channel direct chats with agents."""

from __future__ import annotations

from typing import Final

AGENT_DIRECT_KIND: Final = "agent_direct"


def agent_direct_context_ref(user_email: str, agent_id: str) -> str:
    """Return the stable context reference for a user's direct chat with an agent."""

    return f"web:agent_direct:{user_email}:{agent_id}"


def is_agent_direct_context(context_type: str | None, context_data: object) -> bool:
    """Return whether a conversation context represents an agent direct chat."""

    if context_type != "web" or not isinstance(context_data, dict):
        return False
    return context_data.get("kind") == AGENT_DIRECT_KIND
