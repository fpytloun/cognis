"""Channel type registry — known channel types and their metadata.

Provides static metadata about each supported channel type, including
capabilities, required credentials, and configuration fields.  This is
used by the UI to render channel setup forms and by the manager to
instantiate the correct adapter.
"""

from __future__ import annotations

from cognis.models.channel import (
    ChannelCapabilities,
    ChannelMeta,
    CredentialField,
    SettingField,
)

# ---------------------------------------------------------------------------
# Channel metadata definitions
# ---------------------------------------------------------------------------

SIGNAL_META = ChannelMeta(
    channel_type="signal",
    label="Signal",
    description="Signal Messenger via signal-cli REST API. Requires a linked device.",
    icon="signal",
    docs_url="https://github.com/bbernhard/signal-cli-rest-api",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_typing=True,
        supports_reactions=True,
        supports_read_receipts=True,
        supports_media=True,
        max_message_length=10000,
    ),
    credential_fields=[
        CredentialField(
            name="api_url",
            label="signal-cli REST API URL",
            description="URL of the signal-cli REST API (e.g., http://localhost:8080)",
            secret=False,
        ),
        CredentialField(
            name="account_number",
            label="Phone number",
            description="E.164 phone number linked to signal-cli (e.g., +1234567890)",
            secret=False,
        ),
    ],
    setting_fields=[
        SettingField(
            name="trust_mode",
            label="Trust mode",
            description="How to handle untrusted identities",
            field_type="select",
            default="trust-all-known",
            options=["trust-all-known", "always-trust", "on-first-use"],
        ),
    ],
    connection_mode="sse",
)

WHATSAPP_META = ChannelMeta(
    channel_type="whatsapp",
    label="WhatsApp",
    description="WhatsApp Business Cloud API. Requires a Meta Business account.",
    icon="whatsapp",
    docs_url="https://developers.facebook.com/docs/whatsapp/cloud-api",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_typing=False,
        supports_reactions=True,
        supports_read_receipts=True,
        supports_media=True,
        supports_buttons=True,
        max_message_length=4096,
    ),
    credential_fields=[
        CredentialField(
            name="access_token",
            label="Access token",
            description="Permanent access token from Meta Business",
        ),
        CredentialField(
            name="phone_number_id",
            label="Phone number ID",
            description="WhatsApp Business phone number ID",
            secret=False,
        ),
        CredentialField(
            name="verify_token",
            label="Webhook verify token",
            description="Token for webhook URL verification",
        ),
    ],
    setting_fields=[
        SettingField(
            name="api_version",
            label="API version",
            description="Graph API version",
            default="v21.0",
        ),
    ],
    connection_mode="webhook",
)

TELEGRAM_META = ChannelMeta(
    channel_type="telegram",
    label="Telegram",
    description="Telegram Bot API. Create a bot via @BotFather.",
    icon="telegram",
    docs_url="https://core.telegram.org/bots/api",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group", "channel"],
        supports_threads=True,
        supports_reactions=True,
        supports_edits=True,
        supports_media=True,
        supports_typing=True,
        supports_markdown=True,
        supports_buttons=True,
        max_message_length=4096,
    ),
    credential_fields=[
        CredentialField(
            name="bot_token",
            label="Bot token",
            description="Token from @BotFather",
        ),
    ],
    setting_fields=[
        SettingField(
            name="use_webhook",
            label="Use webhook",
            description="Use webhook instead of long polling",
            field_type="boolean",
            default=False,
        ),
        SettingField(
            name="webhook_url",
            label="Webhook URL",
            description="Public URL for Telegram to send updates (required if webhook enabled)",
        ),
    ],
    connection_mode="long_poll",
)

DISCORD_META = ChannelMeta(
    channel_type="discord",
    label="Discord",
    description="Discord Bot via Gateway WebSocket. Each bot token should be used by one Cognis agent.",
    icon="discord",
    docs_url="https://discord.com/developers/docs",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group", "channel"],
        supports_threads=True,
        supports_reactions=True,
        supports_edits=True,
        supports_media=True,
        supports_typing=True,
        supports_markdown=True,
        supports_buttons=True,
        max_message_length=2000,
    ),
    credential_fields=[
        CredentialField(
            name="bot_token",
            label="Bot token",
            description="Bot token from Discord Developer Portal",
        ),
    ],
    setting_fields=[
        SettingField(
            name="guild_ids",
            label="Server IDs",
            description="Comma-separated Discord server IDs to join (empty = all)",
        ),
    ],
    connection_mode="websocket",
)

SLACK_META = ChannelMeta(
    channel_type="slack",
    label="Slack",
    description="Slack via Socket Mode (preferred) or HTTP Events API. Add chat:write.customize scope for agent identity on messages.",
    icon="slack",
    docs_url="https://api.slack.com/",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group", "channel"],
        supports_threads=True,
        supports_reactions=True,
        supports_edits=True,
        supports_media=True,
        supports_typing=True,
        supports_markdown=True,
        supports_buttons=True,
        max_message_length=4000,
    ),
    credential_fields=[
        CredentialField(
            name="bot_token",
            label="Bot token",
            description="Bot User OAuth Token (xoxb-...)",
        ),
        CredentialField(
            name="app_token",
            label="App-level token",
            description="App-Level Token for Socket Mode (xapp-...)",
        ),
        CredentialField(
            name="signing_secret",
            label="Signing secret",
            description="Signing secret for HTTP webhook verification",
            required=False,
        ),
    ],
    setting_fields=[
        SettingField(
            name="use_socket_mode",
            label="Use Socket Mode",
            description="Use Socket Mode instead of HTTP Events API",
            field_type="boolean",
            default=True,
        ),
    ],
    connection_mode="websocket",
)

MATRIX_META = ChannelMeta(
    channel_type="matrix",
    label="Matrix",
    description="Matrix protocol via matrix-nio. Supports any Matrix homeserver.",
    icon="matrix",
    docs_url="https://matrix.org/docs/",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_threads=True,
        supports_reactions=True,
        supports_edits=True,
        supports_media=True,
        supports_typing=True,
        supports_markdown=True,
        supports_read_receipts=True,
        max_message_length=65536,
    ),
    credential_fields=[
        CredentialField(
            name="homeserver_url",
            label="Homeserver URL",
            description="Matrix homeserver URL (e.g., https://matrix.org)",
            secret=False,
        ),
        CredentialField(
            name="user_id",
            label="User ID",
            description="Matrix user ID (e.g., @bot:matrix.org)",
            secret=False,
        ),
        CredentialField(
            name="access_token",
            label="Access token",
            description="Matrix access token",
        ),
    ],
    connection_mode="long_poll",
)

IRC_META = ChannelMeta(
    channel_type="irc",
    label="IRC",
    description="Internet Relay Chat via asyncio TCP connection.",
    icon="irc",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_typing=False,
        supports_reactions=False,
        supports_edits=False,
        supports_media=False,
        max_message_length=512,
    ),
    credential_fields=[
        CredentialField(
            name="server",
            label="Server",
            description="IRC server hostname (e.g., irc.libera.chat)",
            secret=False,
        ),
        CredentialField(
            name="port",
            label="Port",
            description="IRC server port",
            secret=False,
        ),
        CredentialField(
            name="nickname",
            label="Nickname",
            description="Bot nickname",
            secret=False,
        ),
        CredentialField(
            name="password",
            label="Password",
            description="NickServ or server password",
            required=False,
        ),
    ],
    setting_fields=[
        SettingField(
            name="channels",
            label="Channels",
            description="Comma-separated channels to join (e.g., #general,#dev)",
        ),
        SettingField(
            name="use_tls",
            label="Use TLS",
            field_type="boolean",
            default=True,
        ),
    ],
    connection_mode="long_poll",
)

GOOGLE_CHAT_META = ChannelMeta(
    channel_type="google_chat",
    label="Google Chat",
    description="Google Chat via Chat API. Requires a Google Workspace account.",
    icon="google_chat",
    docs_url="https://developers.google.com/workspace/chat",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_threads=True,
        supports_reactions=True,
        supports_media=True,
        supports_buttons=True,
        max_message_length=4096,
    ),
    credential_fields=[
        CredentialField(
            name="service_account_json",
            label="Service account JSON",
            description="Google Cloud service account credentials JSON",
        ),
        CredentialField(
            name="project_id",
            label="Project ID",
            description="Google Cloud project ID",
            secret=False,
        ),
    ],
    connection_mode="webhook",
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CHANNEL_REGISTRY: dict[str, ChannelMeta] = {
    "signal": SIGNAL_META,
    "whatsapp": WHATSAPP_META,
    "telegram": TELEGRAM_META,
    "discord": DISCORD_META,
    "slack": SLACK_META,
    "matrix": MATRIX_META,
    "irc": IRC_META,
    "google_chat": GOOGLE_CHAT_META,
}


def get_channel_meta(channel_type: str) -> ChannelMeta | None:
    """Look up metadata for a channel type."""
    return CHANNEL_REGISTRY.get(channel_type)


def list_channel_types() -> list[ChannelMeta]:
    """Return metadata for all known channel types."""
    return list(CHANNEL_REGISTRY.values())
