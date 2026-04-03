"""Channel adapter factory — shared between controller and executor."""

from __future__ import annotations

from cognis.channels.protocol import BaseChannelAdapter


def create_adapter(channel_type: str) -> BaseChannelAdapter:
    """Create an adapter instance for a channel type.

    Lazily imports adapter modules to avoid loading unused dependencies.
    Used by both the controller-side ChannelManager and the executor-side
    ChannelHandler.
    """
    if channel_type == "signal":
        from cognis.channels.adapters.signal import SignalAdapter

        return SignalAdapter()
    if channel_type == "whatsapp":
        from cognis.channels.adapters.whatsapp import WhatsAppAdapter

        return WhatsAppAdapter()
    if channel_type == "telegram":
        from cognis.channels.adapters.telegram import TelegramAdapter

        return TelegramAdapter()
    if channel_type == "discord":
        from cognis.channels.adapters.discord import DiscordAdapter

        return DiscordAdapter()
    if channel_type == "slack":
        from cognis.channels.adapters.slack import SlackAdapter

        return SlackAdapter()
    if channel_type == "matrix":
        from cognis.channels.adapters.matrix import MatrixAdapter

        return MatrixAdapter()
    if channel_type == "irc":
        from cognis.channels.adapters.irc import IRCAdapter

        return IRCAdapter()
    if channel_type == "google_chat":
        from cognis.channels.adapters.google_chat import GoogleChatAdapter

        return GoogleChatAdapter()
    msg = f"Unknown channel type: {channel_type}"
    raise ValueError(msg)
