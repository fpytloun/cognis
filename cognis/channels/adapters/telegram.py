"""Telegram adapter via Bot API.

Supports both long polling and webhook modes.  Long polling is the
default; webhook mode requires a public URL.

Required credentials:
- bot_token: Token from @BotFather
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import TELEGRAM_META
from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAdapter(BaseChannelAdapter):
    """Telegram Bot API adapter."""

    channel_type = "telegram"
    capabilities: ChannelCapabilities = TELEGRAM_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._bot_token: str = ""
        self._bot_username: str = ""
        self._last_update_id: int = 0

    async def _connect(self) -> None:
        """Initialize HTTP client and verify bot token."""
        self._bot_token = self._credentials.get("bot_token", "")
        if not self._bot_token:
            msg = "Telegram adapter requires bot_token credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=f"{_TELEGRAM_API_BASE}/bot{self._bot_token}",
            timeout=httpx.Timeout(30.0, read=60.0),  # Long polling needs longer read timeout
        )

        # Verify bot token and get bot info
        resp = await self._client.get("/getMe")
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            msg = f"Telegram getMe failed: {result}"
            raise ValueError(msg)
        self._bot_username = result.get("result", {}).get("username", "")

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """Poll for updates using Telegram long polling."""
        if self._client is None:
            return

        while not self._stop_event.is_set():
            try:
                resp = await self._client.get(
                    "/getUpdates",
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 30,
                        "allowed_updates": json.dumps(["message", "edited_message"]),
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if not data.get("ok"):
                    await asyncio.sleep(1)
                    continue

                for update in data.get("result", []):
                    self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                    await self._handle_update(update)

            except httpx.ReadTimeout:
                continue  # Normal for long polling
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "telegram adapter: polling error",
                    extra={"extra_data": {"account_id": self.account_id}},
                )
                await asyncio.sleep(1)

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via Telegram Bot API."""
        if self._client is None:
            return None

        payload: dict[str, Any] = {
            "chat_id": message.chat_id,
            "text": message.content,
            "parse_mode": "Markdown",
        }

        if message.reply_to_id:
            payload["reply_to_message_id"] = message.reply_to_id

        try:
            resp = await self._client.post("/sendMessage", json=payload)
            # If markdown parsing fails, retry without parse_mode
            if resp.status_code == 400:
                payload.pop("parse_mode", None)
                resp = await self._client.post("/sendMessage", json=payload)
            resp.raise_for_status()
            result = resp.json()
            return str(result.get("result", {}).get("message_id", ""))
        except Exception:
            logger.exception(
                "telegram adapter: send failed",
                extra={"extra_data": {"account_id": self.account_id}},
            )
            return None

    async def sync_profile(self, profile: AgentProfile) -> None:
        if self._client is None:
            return
        try:
            await self._client.post("/setMyName", json={"name": profile.effective_name})
            logger.info(
                "telegram adapter: agent name synced",
                extra={"extra_data": {"account_id": self.account_id}},
            )
        except Exception:
            logger.warning(
                "telegram adapter: name sync failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                "/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify Telegram webhook using secret token header."""
        token = headers.get("x-telegram-bot-api-secret-token", "")
        if not token or not secret:
            return False
        return hmac.compare_digest(token, secret)

    async def handle_webhook_payload(self, body: bytes) -> dict[str, Any] | None:
        """Process a Telegram webhook update."""
        try:
            update = json.loads(body)
        except json.JSONDecodeError:
            return None
        await self._handle_update(update)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Update handling
    # ------------------------------------------------------------------

    async def _handle_update(self, update: dict[str, Any]) -> None:
        """Process a Telegram update."""
        msg = update.get("message") or update.get("edited_message")
        if msg is None:
            return

        # Extract message content
        text = msg.get("text", "")
        caption = msg.get("caption", "")
        content = text or caption

        # Extract sender info
        sender = msg.get("from", {})
        sender_id = str(sender.get("id", ""))
        sender_name = sender.get("first_name", "")
        if sender.get("last_name"):
            sender_name += f" {sender['last_name']}"
        sender_username = sender.get("username")

        # Determine chat type
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        chat_type_raw = chat.get("type", "private")
        chat_type = "direct" if chat_type_raw == "private" else "group"
        chat_name = chat.get("title") or chat.get("first_name")

        # Check for bot mention in groups
        was_mentioned = False
        if chat_type == "group" and self._bot_username:
            # Check entities for @mention
            for entity in msg.get("entities", []):
                if entity.get("type") == "mention":
                    offset = entity.get("offset", 0)
                    length = entity.get("length", 0)
                    mention_text = content[offset : offset + length]
                    if mention_text.lower() == f"@{self._bot_username.lower()}":
                        was_mentioned = True
                        # Strip the mention from content
                        content = (content[:offset] + content[offset + length :]).strip()
                        break

            # Also check for reply to bot
            reply_to = msg.get("reply_to_message", {})
            reply_from = reply_to.get("from", {})
            if reply_from.get("username", "").lower() == self._bot_username.lower():
                was_mentioned = True

        # Parse media
        media: list[MediaAttachment] = []
        for media_type in ("photo", "document", "video", "audio", "voice"):
            media_data = msg.get(media_type)
            if media_data:
                if isinstance(media_data, list):
                    # Photos come as a list of sizes
                    media_data = media_data[-1] if media_data else None
                if media_data:
                    media.append(
                        MediaAttachment(
                            platform_id=media_data.get("file_id"),
                            mime_type=media_data.get("mime_type"),
                            filename=media_data.get("file_name"),
                            size_bytes=media_data.get("file_size"),
                        )
                    )

        if not content and not media:
            return

        # Reply context
        reply_to_id = None
        if msg.get("reply_to_message"):
            reply_to_id = str(msg["reply_to_message"].get("message_id", ""))

        # Thread context
        thread_id = None
        if msg.get("message_thread_id"):
            thread_id = str(msg["message_thread_id"])

        message = InboundMessage(
            channel_type="telegram",
            account_id=self.account_id,
            message_id=str(msg.get("message_id", "")),
            sender_id=sender_id,
            sender_name=sender_name,
            sender_username=sender_username,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            content=content,
            reply_to_id=reply_to_id,
            thread_id=thread_id,
            media=media,
            was_mentioned=was_mentioned,
            timestamp=datetime.fromtimestamp(msg.get("date", 0), tz=UTC),
            platform_data={"update": update},
        )

        await self._dispatch_inbound(message)

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        if self._client is None or not attachment.platform_id:
            return None
        resp = await self._client.get("/getFile", params={"file_id": attachment.platform_id})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return None
        file_path = data.get("result", {}).get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return None
        async with httpx.AsyncClient(timeout=30.0) as client:
            content_resp = await client.get(
                f"{_TELEGRAM_API_BASE}/file/bot{self._bot_token}/{file_path}"
            )
            content_resp.raise_for_status()
        return (
            content_resp.content,
            attachment.mime_type
            or content_resp.headers.get("content-type", "application/octet-stream"),
            attachment.filename or file_path.rsplit("/", 1)[-1],
        )
