"""Predicates for long-lived ambient chat conversations."""

from __future__ import annotations

from typing import Final

from cognis.core.agent_direct import is_agent_direct_context
from cognis.models.session import ConversationContext

AGENT_DIRECT_CONTEXT_REF_PREFIX: Final = "web:agent_direct:"
WEB_MAIN_CONTEXT_REF_PREFIX: Final = "web:user:"
WEB_MAIN_CONTEXT_REF_SUFFIX: Final = ":default"
NON_CHANNEL_CONTEXT_TYPES: Final = frozenset({"api", "chat", "direct", "task", "web"})


def is_agent_direct_context_ref(ref: str | None) -> bool:
    """Return whether *ref* identifies a web agent-direct conversation."""

    return isinstance(ref, str) and ref.startswith(AGENT_DIRECT_CONTEXT_REF_PREFIX)


def is_web_main_chat_context_ref(ref: str | None) -> bool:
    """Return whether *ref* identifies the main web channel conversation."""

    return (
        isinstance(ref, str)
        and ref.startswith(WEB_MAIN_CONTEXT_REF_PREFIX)
        and ref.endswith(WEB_MAIN_CONTEXT_REF_SUFFIX)
    )


def is_web_main_chat_context(context: ConversationContext | None) -> bool:
    """Return whether *context* is the DM-like main web chat surface."""

    if context is None:
        return False
    context_type = str(context.type or "").strip().lower()
    if is_agent_direct_context(context_type, context.platform_data):
        return True
    if context_type != "web":
        return False
    return is_agent_direct_context_ref(context.ref) or is_web_main_chat_context_ref(context.ref)


def is_channel_context_type(context_type: str | None) -> bool:
    """Return whether a context type is an external channel context."""

    normalized = str(context_type or "").strip().lower()
    return bool(normalized) and normalized not in NON_CHANNEL_CONTEXT_TYPES


def is_long_lived_chat_context(context: ConversationContext | None) -> bool:
    """Return whether *context* is an ambient chat that should checkpoint on idle."""

    if context is None:
        return False
    context_type = str(context.type or "").strip().lower()
    if is_web_main_chat_context(context):
        return True
    return is_channel_context_type(context_type)
