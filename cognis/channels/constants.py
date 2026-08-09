"""Shared channel persistence namespaces."""

CHANNEL_TOOL_CONVERSATION_PREFIX = "__channel_tool_delivery__:"
CHANNEL_TOOL_MESSAGE_SOURCE = "channel_tool_message"
CHANNEL_RECIPIENT_MESSAGE_SOURCE = "channel_recipient"
EXPLICIT_CHANNEL_DELIVERY_SOURCES = frozenset(
    {CHANNEL_TOOL_MESSAGE_SOURCE, CHANNEL_RECIPIENT_MESSAGE_SOURCE}
)
ACTIVE_CHANNEL_TOOL_DELIVERY_STATES = frozenset({"pending", "sending", "failed", "uncertain"})
MANAGED_CHANNEL_OBJECTIVE_MAX_CHARS = 1000
