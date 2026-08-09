"""Channel type registry — known channel types and their metadata.

Provides static metadata about each supported channel type, including
capabilities, required credentials, and configuration fields.  This is
used by the UI to render channel setup forms and by the manager to
instantiate the correct adapter.
"""

from __future__ import annotations

from cognis.channels.recipients import ADDRESS_KINDS
from cognis.models.channel import (
    ChannelCapabilities,
    ChannelMeta,
    CredentialField,
    SettingField,
)

_ASSISTANT_DELIVERY_MODE_FIELD = SettingField(
    name="assistant_delivery_mode",
    label="Assistant delivery mode",
    description=(
        "Choose whether channel replies deliver only the final assistant message, "
        "all turn messages concatenated at the end, or buffered chunks while the "
        "assistant is still generating."
    ),
    field_type="select",
    default="final_only",
    options=["final_only", "concatenated", "immediate"],
)

# ---------------------------------------------------------------------------
# Channel metadata definitions
# ---------------------------------------------------------------------------

SIGNAL_META = ChannelMeta(
    channel_type="signal",
    label="Signal",
    description=(
        "Signal Messenger via signal-cli. Supports two transports: "
        "REST API (external signal-cli-rest-api) or direct JSON-RPC "
        "(executor-managed signal-cli process)."
    ),
    icon="signal",
    docs_url="https://github.com/AsamK/signal-cli",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_typing=True,
        supports_reactions=True,
        supports_read_receipts=True,
        supports_media=True,
        supports_markdown=True,
        inbound_ordering="provider",
        max_message_length=10000,
    ),
    credential_fields=[
        CredentialField(
            name="api_url",
            label="signal-cli REST API URL",
            description=(
                "URL of the signal-cli REST API (e.g., http://localhost:8080). "
                "Required for REST API transport; ignored for direct JSON-RPC."
            ),
            secret=False,
            required=False,
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
            name="transport",
            label="Transport",
            description=(
                "How to connect to signal-cli. 'rest_api' uses an external "
                "signal-cli REST API; 'direct_jsonrpc' runs signal-cli directly "
                "on the executor via JSON-RPC (executor-only)."
            ),
            field_type="select",
            default="rest_api",
            options=["rest_api", "direct_jsonrpc"],
        ),
        SettingField(
            name="trust_mode",
            label="Trust mode",
            description="How to handle untrusted identities",
            field_type="select",
            default="trust-all-known",
            options=["trust-all-known", "always-trust", "on-first-use"],
        ),
        SettingField(
            name="send_read_receipts",
            label="Send read receipts",
            description="Send read receipts when messages are processed",
            field_type="boolean",
            default=True,
        ),
        SettingField(
            name="enable_typing",
            label="Typing indicators",
            description="Send typing indicators while generating responses",
            field_type="boolean",
            default=True,
        ),
        SettingField(
            name="sync_profile",
            label="Sync profile",
            description="Sync agent name and avatar to Signal profile",
            field_type="boolean",
            default=True,
        ),
        SettingField(
            name="ignore_stories",
            label="Ignore stories",
            description="Ignore Signal story messages",
            field_type="boolean",
            default=True,
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
    description="Matrix protocol via the Client-Server API. Supports unencrypted rooms on any Matrix homeserver.",
    icon="matrix",
    docs_url="https://matrix.org/docs/",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_threads=True,
        supports_reactions=False,
        supports_edits=False,
        supports_media=True,
        supports_typing=True,
        supports_markdown=True,
        supports_sanitized_html=True,
        supports_inline_media=True,
        supports_read_receipts=True,
        supports_idempotent_send=True,
        inbound_ordering="provider",
        max_message_length=None,
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
            required=False,
            secret=False,
        ),
        CredentialField(
            name="access_token",
            label="Access token",
            description="Matrix access token. Preferred for stable bot accounts.",
            required=False,
        ),
        CredentialField(
            name="username",
            label="Username",
            description="Optional Matrix username for password login when no access token is provided.",
            required=False,
            secret=False,
        ),
        CredentialField(
            name="password",
            label="Password",
            description="Optional Matrix password for login when no access token is provided.",
            required=False,
        ),
        CredentialField(
            name="device_id",
            label="Device ID",
            description="Optional device ID used for password login.",
            required=False,
            secret=False,
        ),
    ],
    setting_fields=[
        SettingField(
            name="require_mention",
            label="Require mention in group rooms",
            description="Ignore group-room messages unless they mention the Matrix bot account.",
            field_type="boolean",
            default=False,
        ),
        SettingField(
            name="auto_join_invites",
            label="Auto-join invited rooms",
            description=(
                "Automatically join rooms where the bot account is invited. "
                "When Allowed group rooms is set, only those room IDs are auto-joined; "
                "invite metadata is not trusted to distinguish DMs from groups."
            ),
            field_type="boolean",
            default=False,
        ),
        SettingField(
            name="allowed_rooms",
            label="Allowed group rooms",
            description=(
                "Optional comma-separated allowlist of Matrix group room IDs. "
                "Listed rooms are treated as group rooms; DM rooms are exempt "
                "unless listed here and are otherwise controlled by DM policy."
            ),
        ),
        SettingField(
            name="direct_rooms",
            label="Rooms treated as DM",
            description=(
                "Optional comma-separated Matrix room IDs to treat as direct messages "
                "when Matrix direct-room metadata or member counts are missing."
            ),
        ),
        SettingField(
            name="group_rooms",
            label="Rooms treated as group",
            description=(
                "Optional comma-separated Matrix room IDs to treat as group rooms even "
                "when they only have two members. Use this for small private rooms where "
                "group-room policies and thread mode should apply."
            ),
        ),
        SettingField(
            name="dm_conversation_mode",
            label="DM conversation mode",
            description=(
                "'default' continues one DM conversation and forks Matrix threads when needed. "
                "'threads' creates a Matrix thread/session for each accepted top-level user message."
            ),
            field_type="select",
            default="default",
            options=["default", "threads"],
        ),
        SettingField(
            name="group_conversation_mode",
            label="Group conversation mode",
            description=(
                "'default' continues one room conversation and forks Matrix threads when needed. "
                "'threads' creates a Matrix thread/session for each accepted top-level user message."
            ),
            field_type="select",
            default="default",
            options=["default", "threads"],
        ),
        SettingField(
            name="thread_start_mode",
            label="Thread start mode",
            description=(
                "Only used in default conversation mode. 'fork' starts a Matrix thread from the exact "
                "source turn when available; 'fresh' starts a new session seeded with root-message context."
            ),
            field_type="select",
            default="fork",
            options=["fork", "fresh"],
        ),
        SettingField(
            name="ignored_senders",
            label="Ignored senders",
            description="Optional comma-separated Matrix user IDs to ignore.",
        ),
        SettingField(
            name="ignored_sender_patterns",
            label="Ignored sender patterns",
            description="Optional comma-separated regular expressions for Matrix user IDs to ignore.",
        ),
        SettingField(
            name="suppress_startup_replay",
            label="Suppress startup replay",
            description="Drop old timeline messages during adapter startup.",
            field_type="boolean",
            default=True,
        ),
        SettingField(
            name="ignore_appservice_senders",
            label="Ignore appservice senders",
            description="Ignore Matrix appservice sender IDs that start with @_.",
            field_type="boolean",
            default=True,
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


BLUEBUBBLES_META = ChannelMeta(
    channel_type="bluebubbles",
    label="iMessage (BlueBubbles)",
    description=(
        "iMessage via BlueBubbles server on macOS. "
        "Outbound messages use the BlueBubbles REST API; "
        "inbound messages arrive via webhooks."
    ),
    icon="imessage",
    docs_url="https://bluebubbles.app",
    capabilities=ChannelCapabilities(
        chat_types=["direct", "group"],
        supports_typing=True,
        supports_read_receipts=True,
        supports_media=True,
        supports_reactions=False,
        max_message_length=20000,
    ),
    credential_fields=[
        CredentialField(
            name="server_url",
            label="BlueBubbles server URL",
            description=(
                "URL of the BlueBubbles server "
                "(e.g., http://192.168.1.100:1234 or https://your-bb.ngrok.io)"
            ),
            secret=False,
        ),
        CredentialField(
            name="password",
            label="API password",
            description="BlueBubbles server API password",
        ),
    ],
    setting_fields=[
        SettingField(
            name="send_read_receipts",
            label="Send read receipts",
            description="Send read receipts when messages are processed",
            field_type="boolean",
            default=True,
        ),
        SettingField(
            name="enable_typing",
            label="Typing indicators",
            description="Send typing indicators while generating responses",
            field_type="boolean",
            default=True,
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
    "bluebubbles": BLUEBUBBLES_META,
}

_RECIPIENT_RESOLUTION_CHANNELS = {
    "telegram",
    "slack",
    "matrix",
    "google_chat",
    "bluebubbles",
}
_RECIPIENT_CREATION_CHANNELS = {
    "discord",
    "slack",
    "matrix",
}
_RECIPIENT_DIRECT_ONLY_CHANNELS = {"whatsapp"}

for _meta in CHANNEL_REGISTRY.values():
    _recipient_capabilities = _meta.capabilities.recipient_capabilities
    _recipient_capabilities.address_kinds = list(ADDRESS_KINDS.get(_meta.channel_type, ()))
    _recipient_capabilities.chat_kinds = (
        ["direct"] if _meta.channel_type in _RECIPIENT_DIRECT_ONLY_CHANNELS else ["direct", "group"]
    )
    _recipient_capabilities.supports_resolution = (
        _meta.channel_type in _RECIPIENT_RESOLUTION_CHANNELS
    )
    _recipient_capabilities.supports_creation = _meta.channel_type in _RECIPIENT_CREATION_CHANNELS
    if not any(field.name == "assistant_delivery_mode" for field in _meta.setting_fields):
        _meta.setting_fields.append(_ASSISTANT_DELIVERY_MODE_FIELD.model_copy())


def get_channel_meta(channel_type: str) -> ChannelMeta | None:
    """Look up metadata for a channel type."""
    return CHANNEL_REGISTRY.get(channel_type)


def list_channel_types() -> list[ChannelMeta]:
    """Return metadata for all known channel types."""
    return list(CHANNEL_REGISTRY.values())
