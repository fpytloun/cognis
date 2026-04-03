"""Signal adapter via signal-cli REST API.

Uses the signal-cli REST API (https://github.com/bbernhard/signal-cli-rest-api)
for both sending and receiving.  Inbound messages are received via SSE
(Server-Sent Events) stream.  Outbound messages use the REST API.

Required credentials:
- api_url: URL of the signal-cli REST API
- account_number: E.164 phone number linked to signal-cli
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import SIGNAL_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)


class SignalAdapter(BaseChannelAdapter):
    """Signal Messenger adapter via signal-cli REST API."""

    channel_type = "signal"
    capabilities: ChannelCapabilities = SIGNAL_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._api_url: str = ""
        self._account_number: str = ""

    async def _connect(self) -> None:
        """Initialize HTTP client for signal-cli REST API."""
        self._api_url = self._credentials.get("api_url", "").rstrip("/")
        self._account_number = self._credentials.get("account_number", "")

        if not self._api_url:
            msg = "Signal adapter requires api_url credential"
            raise ValueError(msg)
        if not self._account_number:
            msg = "Signal adapter requires account_number credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=self._api_url,
            timeout=httpx.Timeout(30.0, read=None),  # SSE needs no read timeout
        )

        # Verify connectivity
        resp = await self._client.get("/v1/about")
        resp.raise_for_status()

    async def _disconnect(self) -> None:
        """Close HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """Listen for inbound messages via SSE stream."""
        if self._client is None:
            return

        url = f"/v1/receive/{self._account_number}"

        async with self._client.stream(
            "GET", url, headers={"Accept": "text/event-stream"}
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if self._stop_event.is_set():
                    break

                if not line or not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if not data_str:
                    continue

                try:
                    event_data = json.loads(data_str)
                    await self._handle_event(event_data)
                except json.JSONDecodeError:
                    logger.warning(
                        "signal adapter: invalid JSON in SSE event",
                        extra={"extra_data": {"account_id": self.account_id}},
                    )
                except Exception:
                    logger.exception(
                        "signal adapter: error handling SSE event",
                        extra={"extra_data": {"account_id": self.account_id}},
                    )

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via signal-cli REST API."""
        if self._client is None:
            return None

        payload: dict[str, Any] = {
            "message": message.content,
            "number": self._account_number,
            "recipients": [message.chat_id],
        }

        if message.reply_to_id:
            payload["quote_timestamp"] = message.reply_to_id

        resp = await self._client.post("/v2/send", json=payload)
        resp.raise_for_status()
        result = resp.json()
        return result.get("timestamp")

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._client is None:
            return
        payload = {
            "recipient": chat_id,
            "number": self._account_number,
        }
        with contextlib.suppress(Exception):
            await self._client.put("/v1/typing-indicator/" + self._account_number, json=payload)

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Mark a message as read."""
        if self._client is None:
            return
        payload = {
            "recipient": chat_id,
            "timestamps": [message_id],
        }
        with contextlib.suppress(Exception):
            await self._client.post(
                f"/v1/receipts/{self._account_number}",
                json=payload,
            )

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Process a signal-cli event."""
        envelope = event.get("envelope", {})
        if not envelope:
            return

        # Only handle data messages
        data_message = envelope.get("dataMessage")
        if data_message is None:
            return

        source = envelope.get("source", "")
        source_name = envelope.get("sourceName", "")
        timestamp = data_message.get("timestamp", 0)
        body = data_message.get("message", "")

        if not body and not data_message.get("attachments"):
            return

        # Determine chat type and ID
        group_info = data_message.get("groupInfo")
        if group_info:
            chat_id = group_info.get("groupId", "")
            chat_type = "group"
            chat_name = group_info.get("groupName")
        else:
            chat_id = source
            chat_type = "direct"
            chat_name = source_name

        # Parse attachments
        media: list[MediaAttachment] = []
        for attachment in data_message.get("attachments", []):
            media.append(
                MediaAttachment(
                    path=attachment.get("filename"),
                    mime_type=attachment.get("contentType"),
                    size_bytes=attachment.get("size"),
                )
            )

        # Check for mentions (for group policy)
        was_mentioned = False
        for mention in data_message.get("mentions", []):
            if mention.get("number") == self._account_number:
                was_mentioned = True
                break

        message = InboundMessage(
            channel_type="signal",
            account_id=self.account_id,
            message_id=str(timestamp),
            sender_id=source,
            sender_name=source_name,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            content=body or "",
            media=media,
            was_mentioned=was_mentioned,
            timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC)
            if timestamp
            else datetime.now(UTC),
            platform_data={"envelope": envelope},
        )

        await self._dispatch_inbound(message)
