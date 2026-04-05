"""BlueBubbles adapter for iMessage via BlueBubbles server REST API + webhooks.

Outbound messages, typing indicators, and read receipts use the
BlueBubbles REST API.  Inbound messages arrive via webhooks that
BlueBubbles POSTs to the Cognis webhook endpoint.

Required credentials:
- server_url: URL of the BlueBubbles server (e.g., http://192.168.1.100:1234)
- password: BlueBubbles server API password
"""

from __future__ import annotations

import contextlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import BLUEBUBBLES_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)

_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB
_ATTACHMENT_TIMEOUT_S = 60.0
_API_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Internal typed config
# ---------------------------------------------------------------------------


class _BlueBubblesConfig:
    """Parsed BlueBubbles adapter configuration with safe defaults."""

    def __init__(self, settings: dict[str, Any], credentials: dict[str, str]) -> None:
        self.server_url: str = credentials.get("server_url", "").rstrip("/")
        self.password: str = credentials.get("password", "")
        self.send_read_receipts: bool = _bool(settings.get("send_read_receipts", True))
        self.enable_typing: bool = _bool(settings.get("enable_typing", True))


def _bool(value: Any) -> bool:
    """Coerce a value to bool, handling string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


# ---------------------------------------------------------------------------
# BlueBubbles adapter
# ---------------------------------------------------------------------------


class BlueBubblesAdapter(BaseChannelAdapter):
    """iMessage adapter via BlueBubbles server REST API + webhooks."""

    channel_type = "bluebubbles"
    capabilities: ChannelCapabilities = BLUEBUBBLES_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._bb_config: _BlueBubblesConfig | None = None
        self._seen_guids: set[str] = set()
        self._seen_guids_max = 2000

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Initialize HTTP client and verify BlueBubbles connectivity."""
        settings = self._config.settings if self._config else {}
        self._bb_config = _BlueBubblesConfig(settings, self._credentials)

        if not self._bb_config.server_url:
            msg = "BlueBubbles adapter requires server_url credential"
            raise ValueError(msg)
        if not self._bb_config.password:
            msg = "BlueBubbles adapter requires password credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=self._bb_config.server_url,
            timeout=_API_TIMEOUT_S,
        )

        # Health check
        resp = await self._client.get(
            "/api/v1/ping",
            params={"password": self._bb_config.password},
        )
        resp.raise_for_status()

    async def _disconnect(self) -> None:
        """Close HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """BlueBubbles uses webhooks — no long-running connection needed."""
        await self._stop_event.wait()

    # ------------------------------------------------------------------
    # Outbound — send message
    # ------------------------------------------------------------------

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via BlueBubbles REST API."""
        if self._client is None or self._bb_config is None:
            return None

        # Send media attachments first
        for media in message.media:
            await self._send_attachment(message.chat_id, media)

        # Skip text send if only media and no meaningful text
        if not message.content.strip() and message.media:
            return None

        payload: dict[str, Any] = {
            "chatGuid": message.chat_id,
            "message": message.content,
            "method": "private-api",
        }

        try:
            resp = await self._client.post(
                "/api/v1/message/text",
                params={"password": self._bb_config.password},
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            data = result.get("data", {})
            return data.get("guid")
        except Exception:
            logger.warning(
                "bluebubbles adapter: send failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )
            return None

    async def _send_attachment(self, chat_guid: str, media: MediaAttachment) -> None:
        """Send a media attachment via BlueBubbles."""
        if self._client is None or self._bb_config is None or not media.url:
            return
        try:
            async with httpx.AsyncClient(timeout=_ATTACHMENT_TIMEOUT_S) as dl:
                resp = await dl.get(media.url)
                resp.raise_for_status()
                content = resp.content

            if len(content) > _MAX_ATTACHMENT_BYTES:
                logger.warning(
                    "bluebubbles adapter: attachment too large, skipping",
                    extra={
                        "extra_data": {
                            "account_id": self.account_id,
                            "size": len(content),
                        }
                    },
                )
                return

            filename = media.filename or "attachment"
            content_type = media.mime_type or "application/octet-stream"

            resp = await self._client.post(
                "/api/v1/message/attachment",
                params={"password": self._bb_config.password},
                data={"chatGuid": chat_guid},
                files={"attachment": (filename, content, content_type)},
            )
            resp.raise_for_status()
        except Exception:
            logger.warning(
                "bluebubbles adapter: attachment send failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Typing / read receipts
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator via BlueBubbles private API."""
        if self._client is None or self._bb_config is None:
            return
        if not self._bb_config.enable_typing:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                f"/api/v1/chat/{chat_id}/typing",
                params={"password": self._bb_config.password},
                json={"status": "typing"},
            )

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Mark a chat as read via BlueBubbles."""
        if self._client is None or self._bb_config is None:
            return
        if not self._bb_config.send_read_receipts:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                f"/api/v1/chat/{chat_id}/read",
                params={"password": self._bb_config.password},
            )

    # ------------------------------------------------------------------
    # Webhook verification + payload handling
    # ------------------------------------------------------------------

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify BlueBubbles webhook authentication.

        BlueBubbles authenticates webhook requests using a password/guid
        query parameter or header.  The ``secret`` here is the webhook
        secret stored on the Cognis channel account (which should match
        the BlueBubbles API password).
        """
        if not secret:
            return False

        # BlueBubbles authenticates via password/guid in query params or
        # headers.  The webhook route injects query params as x-query-*
        # headers so they are available here.
        candidate = (
            headers.get("x-query-password", "")
            or headers.get("x-query-guid", "")
            or headers.get("x-query-token", "")
            or headers.get("x-password", "")
            or headers.get("x-guid", "")
            or headers.get("password", "")
            or headers.get("guid", "")
        )

        if not candidate:
            return False

        return hmac.compare_digest(candidate, secret)

    async def handle_webhook_payload(self, body: bytes) -> dict[str, Any] | None:
        """Process a BlueBubbles webhook payload."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None

        event_type = data.get("type", "")

        # Only handle new message events
        if event_type not in ("new-message", "updated-message"):
            return {"status": "ignored", "event_type": event_type}

        message_data = data.get("data", {})
        if not message_data:
            return {"status": "ignored"}

        await self._handle_message(message_data)
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Inbound message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """Normalize a BlueBubbles message into an InboundMessage."""
        # Skip self-sent messages to avoid loops
        if msg.get("isFromMe", False):
            return

        guid = msg.get("guid", "")

        # Deduplicate: BlueBubbles may retry webhook delivery
        if guid and guid in self._seen_guids:
            return
        if guid:
            self._seen_guids.add(guid)
            # Bound the set size
            if len(self._seen_guids) > self._seen_guids_max:
                # Remove oldest entries (set doesn't preserve order,
                # but this is good enough for bounded memory)
                excess = len(self._seen_guids) - self._seen_guids_max // 2
                for _ in range(excess):
                    self._seen_guids.pop()

        text = msg.get("text", "") or ""
        subject = msg.get("subject", "")

        # Parse attachments
        media: list[MediaAttachment] = []
        for att in msg.get("attachments", []):
            media.append(
                MediaAttachment(
                    platform_id=att.get("guid"),
                    mime_type=att.get("mimeType"),
                    filename=att.get("transferName"),
                    size_bytes=att.get("totalBytes"),
                )
            )

        if not text and not subject and not media:
            return

        # Determine chat info
        chats = msg.get("chats", [])
        chat = chats[0] if chats else {}
        chat_guid = chat.get("guid", "")
        chat_display_name = chat.get("displayName")
        group_id = chat.get("groupId")

        # Determine chat type
        chat_type = "group" if group_id or (chat_guid and ";+;" in chat_guid) else "direct"

        # Sender info
        handle = msg.get("handle", {}) or {}
        sender_id = handle.get("address", "") or msg.get("address", "")
        sender_name = handle.get("firstName", "")
        if handle.get("lastName"):
            sender_name = f"{sender_name} {handle['lastName']}".strip()

        # Use chat GUID as stable chat identifier
        chat_id = chat_guid or sender_id

        # Build content
        content = text
        if subject and text:
            content = f"{subject}: {text}"
        elif subject:
            content = subject

        # Timestamp
        date_created = msg.get("dateCreated")
        if isinstance(date_created, (int, float)):
            timestamp = datetime.fromtimestamp(date_created / 1000, tz=UTC)
        else:
            timestamp = datetime.now(UTC)

        message = InboundMessage(
            channel_type="bluebubbles",
            account_id=self.account_id,
            message_id=guid,
            sender_id=sender_id,
            sender_name=sender_name or None,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_display_name,
            content=content,
            media=media,
            timestamp=timestamp,
            platform_data={"guid": guid, "chat_guid": chat_guid},
        )

        await self._dispatch_inbound(message)

    # ------------------------------------------------------------------
    # Attachment download
    # ------------------------------------------------------------------

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        """Download an inbound attachment from BlueBubbles."""
        if self._client is None or self._bb_config is None:
            return None

        att_guid = attachment.platform_id
        if not att_guid:
            return None

        try:
            resp = await self._client.get(
                f"/api/v1/attachment/{att_guid}/download",
                params={"password": self._bb_config.password},
                timeout=_ATTACHMENT_TIMEOUT_S,
            )
            resp.raise_for_status()
            content = resp.content

            if len(content) > _MAX_ATTACHMENT_BYTES:
                logger.warning(
                    "bluebubbles adapter: downloaded attachment too large",
                    extra={
                        "extra_data": {
                            "account_id": self.account_id,
                            "size": len(content),
                        }
                    },
                )
                return None

            content_type = attachment.mime_type or resp.headers.get(
                "content-type", "application/octet-stream"
            )
            filename = attachment.filename or att_guid

            return (content, content_type, filename)
        except Exception:
            logger.warning(
                "bluebubbles adapter: attachment download failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )
            return None
