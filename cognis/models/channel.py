"""Domain models for channels and messaging platform adapters."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Channel types — free-form string with known constants
# ---------------------------------------------------------------------------

KNOWN_CHANNEL_TYPES: frozenset[str] = frozenset(
    {
        "signal",
        "whatsapp",
        "telegram",
        "discord",
        "slack",
        "matrix",
        "irc",
        "google_chat",
    }
)


# ---------------------------------------------------------------------------
# Channel capabilities
# ---------------------------------------------------------------------------


class ChannelCapabilities(BaseModel):
    """Declares what a channel adapter can do."""

    chat_types: list[str] = Field(default_factory=lambda: ["direct", "group"])
    supports_threads: bool = False
    supports_reactions: bool = False
    supports_edits: bool = False
    supports_media: bool = False
    supports_typing: bool = False
    supports_read_receipts: bool = False
    supports_markdown: bool = False
    supports_buttons: bool = False
    max_message_length: int = 4096


# ---------------------------------------------------------------------------
# Channel status
# ---------------------------------------------------------------------------


class ChannelStatus(StrEnum):
    """Runtime connection status for a channel account."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"
    STOPPED = "stopped"


class PairingRequestStatus(StrEnum):
    """Lifecycle states for channel pairing requests."""

    PENDING = "pending"
    COMPLETED = "completed"
    EXPIRED = "expired"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Channel account config (DB-stored)
# ---------------------------------------------------------------------------


class ChannelAccountConfig(BaseModel):
    """Configuration for a single channel account.

    Stored in the ``channel_accounts`` DB table.  Credentials are
    stored via SecretsProvider and referenced by name in
    ``credential_refs``.
    """

    account_id: str
    channel_type: str
    display_name: str
    enabled: bool = True

    # Credential references (secret names, not raw values)
    credential_refs: dict[str, str] = Field(default_factory=dict)

    # Agent binding
    agent_id: str
    user_email: str

    # Channel-specific settings (non-sensitive)
    settings: dict[str, Any] = Field(default_factory=dict)

    # Routing
    default_conversation_id: str | None = None
    allow_new_conversations: bool = True

    # Adapter location
    adapter_location: Literal["controller", "executor"] = "controller"
    executor_id: str | None = None  # preferred executor (null = any with channels capability)

    # Access control
    allowed_senders: list[str] = Field(default_factory=list)
    dm_policy: Literal["open", "pairing", "allowlist", "disabled"] = "pairing"
    group_policy: Literal["open", "pairing", "mention", "allowlist", "disabled"] = "pairing"

    # Webhook (for platforms that push)
    webhook_secret: str | None = None


# ---------------------------------------------------------------------------
# Channel account status (runtime, in-memory only)
# ---------------------------------------------------------------------------


class ChannelAccountStatus(BaseModel):
    """Runtime status snapshot for a channel account."""

    account_id: str
    channel_type: str
    status: ChannelStatus = ChannelStatus.DISCONNECTED
    enabled: bool = True
    connected_at: datetime | None = None
    last_message_at: datetime | None = None
    last_error: str | None = None
    reconnect_attempts: int = 0
    active_chats: int = 0


# ---------------------------------------------------------------------------
# Inbound / outbound message models
# ---------------------------------------------------------------------------


class MediaAttachment(BaseModel):
    """A media attachment on a message."""

    url: str | None = None
    path: str | None = None
    platform_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None


class InboundMessage(BaseModel):
    """Normalized inbound message from any channel.

    Every channel adapter normalizes platform-specific events into this
    model before handing off to the inbound pipeline.
    """

    channel_type: str
    account_id: str
    message_id: str

    # Sender identity (platform-specific)
    sender_id: str
    sender_name: str | None = None
    sender_username: str | None = None

    # Chat context
    chat_id: str
    chat_type: str = "direct"  # "direct" or "group"
    chat_name: str | None = None

    # Content
    content: str
    reply_to_id: str | None = None
    thread_id: str | None = None
    media: list[MediaAttachment] = Field(default_factory=list)

    # Platform-specific metadata (never logged)
    platform_data: dict[str, Any] = Field(default_factory=dict)

    timestamp: datetime

    # Was the bot explicitly mentioned (for group policy)
    was_mentioned: bool = False


class OutboundMessage(BaseModel):
    """Message to send to a channel."""

    channel_type: str
    account_id: str
    chat_id: str
    content: str
    reply_to_id: str | None = None
    thread_id: str | None = None
    media: list[MediaAttachment] = Field(default_factory=list)
    platform_data: dict[str, Any] = Field(default_factory=dict)


class AgentProfile(BaseModel):
    """Resolved agent identity for channel adapters."""

    name: str
    display_name: str | None = None
    avatar_url: str | None = None
    avatar_bytes: bytes | None = None
    avatar_content_type: str | None = None

    @property
    def effective_name(self) -> str:
        return self.display_name or self.name


class PairingRequest(BaseModel):
    """A pending or completed external-channel pairing challenge."""

    request_id: str
    owner_email: str
    account_id: str
    channel_type: str
    sender_id: str
    sender_name: str | None = None
    chat_id: str
    chat_name: str | None = None
    code: str
    status: PairingRequestStatus
    attempts: int = 0
    expires_at: datetime
    created_at: datetime
    completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Channel metadata (static, for registry)
# ---------------------------------------------------------------------------


class ChannelMeta(BaseModel):
    """Static metadata about a channel type."""

    channel_type: str
    label: str
    description: str
    icon: str | None = None
    docs_url: str | None = None
    capabilities: ChannelCapabilities
    credential_fields: list[CredentialField] = Field(default_factory=list)
    setting_fields: list[SettingField] = Field(default_factory=list)
    connection_mode: str = "long_poll"  # "webhook", "long_poll", "websocket", "sse"


class CredentialField(BaseModel):
    """Describes a credential required by a channel type."""

    name: str
    label: str
    description: str = ""
    required: bool = True
    secret: bool = True


class SettingField(BaseModel):
    """Describes a non-sensitive setting for a channel type."""

    name: str
    label: str
    description: str = ""
    field_type: str = "text"  # "text", "number", "boolean", "select"
    default: Any = None
    options: list[str] | None = None
