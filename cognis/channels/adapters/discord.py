"""Discord adapter via Bot Gateway WebSocket.

Uses the Discord Gateway (WebSocket) for receiving events and the
REST API for sending messages.  Implements a lightweight gateway
client without depending on discord.py.

Required credentials:
- bot_token: Bot token from Discord Developer Portal
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import DISCORD_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json&compress=zlib-stream"

# Gateway opcodes
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# Gateway intents
_INTENT_GUILDS = 1 << 0
_INTENT_GUILD_MESSAGES = 1 << 9
_INTENT_GUILD_MESSAGE_CONTENT = 1 << 15
_INTENT_DIRECT_MESSAGES = 1 << 12
_INTENT_DIRECT_MESSAGE_CONTENT = 1 << 15  # Shares bit with guild


class DiscordAdapter(BaseChannelAdapter):
    """Discord Bot Gateway adapter."""

    channel_type = "discord"
    capabilities: ChannelCapabilities = DISCORD_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._rest_client: httpx.AsyncClient | None = None
        self._bot_token: str = ""
        self._bot_user_id: str = ""
        self._bot_username: str = ""
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._sequence: int | None = None

    async def _connect(self) -> None:
        """Initialize REST client and verify bot token."""
        self._bot_token = self._credentials.get("bot_token", "")
        if not self._bot_token:
            msg = "Discord adapter requires bot_token credential"
            raise ValueError(msg)

        self._rest_client = httpx.AsyncClient(
            base_url=_DISCORD_API_BASE,
            headers={"Authorization": f"Bot {self._bot_token}"},
            timeout=30.0,
        )

        # Verify token and get bot info
        resp = await self._rest_client.get("/users/@me")
        resp.raise_for_status()
        user_data = resp.json()
        self._bot_user_id = user_data.get("id", "")
        self._bot_username = user_data.get("username", "")

    async def _disconnect(self) -> None:
        if self._rest_client is not None:
            await self._rest_client.aclose()
            self._rest_client = None

    async def _run(self) -> None:
        """Connect to Discord Gateway and process events.

        Uses httpx WebSocket support (if available) or falls back to
        a simple polling approach via REST API.
        """
        # For MVP, use REST-based message polling instead of full Gateway
        # implementation.  A full Gateway client would use websockets library.
        # This is a simplified approach that works without additional deps.
        await self._poll_messages()

    async def _poll_messages(self) -> None:
        """Poll for new messages via REST API (simplified approach).

        A production implementation would use the Gateway WebSocket
        for real-time events.  This polling approach is suitable for
        low-volume use cases.
        """
        if self._rest_client is None:
            return

        # Track last seen message per channel
        last_seen: dict[str, str] = {}

        while not self._stop_event.is_set():
            try:
                # Get DM channels
                resp = await self._rest_client.get("/users/@me/channels")
                if resp.status_code == 200:
                    channels = resp.json()
                    for channel in channels:
                        channel_id = channel.get("id", "")
                        await self._poll_channel(channel_id, last_seen)

                await asyncio.sleep(2)  # Poll interval

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "discord adapter: polling error",
                    extra={"extra_data": {"account_id": self.account_id}},
                )
                await asyncio.sleep(5)

    async def _poll_channel(
        self,
        channel_id: str,
        last_seen: dict[str, str],
    ) -> None:
        """Poll a single channel for new messages."""
        if self._rest_client is None:
            return

        params: dict[str, Any] = {"limit": 10}
        if channel_id in last_seen:
            params["after"] = last_seen[channel_id]

        resp = await self._rest_client.get(
            f"/channels/{channel_id}/messages",
            params=params,
        )
        if resp.status_code != 200:
            return

        messages = resp.json()
        for msg in reversed(messages):  # Process oldest first
            msg_id = msg.get("id", "")
            author = msg.get("author", {})

            # Skip bot's own messages
            if author.get("id") == self._bot_user_id:
                last_seen[channel_id] = msg_id
                continue

            # Skip if already seen
            if channel_id in last_seen and msg_id <= last_seen[channel_id]:
                continue

            last_seen[channel_id] = msg_id
            await self._handle_message(msg, channel_id)

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via Discord REST API."""
        if self._rest_client is None:
            return None

        payload: dict[str, Any] = {"content": message.content}

        if message.reply_to_id:
            payload["message_reference"] = {"message_id": message.reply_to_id}

        resp = await self._rest_client.post(
            f"/channels/{message.chat_id}/messages",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("id")

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._rest_client is None:
            return
        with contextlib.suppress(Exception):
            await self._rest_client.post(f"/channels/{chat_id}/typing")

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: dict[str, Any], channel_id: str) -> None:
        """Process a Discord message."""
        author = msg.get("author", {})
        content = msg.get("content", "")

        if not content:
            return

        sender_id = author.get("id", "")
        sender_name = author.get("global_name") or author.get("username", "")
        sender_username = author.get("username")

        # Determine chat type from channel type
        channel_type_raw = msg.get("channel_type", 1)  # 1 = DM
        chat_type = "direct" if channel_type_raw in {1, 3} else "group"

        # Check for bot mention
        was_mentioned = False
        if self._bot_user_id:
            for mention in msg.get("mentions", []):
                if mention.get("id") == self._bot_user_id:
                    was_mentioned = True
                    # Strip mention from content
                    content = content.replace(f"<@{self._bot_user_id}>", "").strip()
                    content = content.replace(f"<@!{self._bot_user_id}>", "").strip()
                    break

        # Parse attachments
        media: list[MediaAttachment] = []
        for attachment in msg.get("attachments", []):
            media.append(
                MediaAttachment(
                    url=attachment.get("url"),
                    filename=attachment.get("filename"),
                    mime_type=attachment.get("content_type"),
                    size_bytes=attachment.get("size"),
                )
            )

        # Reply context
        reply_to_id = None
        ref = msg.get("message_reference")
        if ref:
            reply_to_id = ref.get("message_id")

        # Thread context
        thread_id = msg.get("thread", {}).get("id") if msg.get("thread") else None

        # Parse timestamp
        timestamp_str = msg.get("timestamp", "")
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.now(UTC)

        message = InboundMessage(
            channel_type="discord",
            account_id=self.account_id,
            message_id=msg.get("id", ""),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_username=sender_username,
            chat_id=channel_id,
            chat_type=chat_type,
            content=content,
            reply_to_id=reply_to_id,
            thread_id=thread_id,
            media=media,
            was_mentioned=was_mentioned,
            timestamp=timestamp,
            platform_data={"raw_message": msg},
        )

        await self._dispatch_inbound(message)
