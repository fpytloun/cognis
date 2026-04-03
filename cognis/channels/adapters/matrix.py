"""Matrix adapter via HTTP Client-Server API.

Uses the Matrix Client-Server API directly (no matrix-nio dependency).
Connects to any Matrix homeserver and syncs for events.

Required credentials:
- homeserver_url: Matrix homeserver URL
- user_id: Matrix user ID (e.g., @bot:matrix.org)
- access_token: Matrix access token
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import MATRIX_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)


class MatrixAdapter(BaseChannelAdapter):
    """Matrix protocol adapter via Client-Server API."""

    channel_type = "matrix"
    capabilities: ChannelCapabilities = MATRIX_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._homeserver_url: str = ""
        self._user_id: str = ""
        self._access_token: str = ""
        self._next_batch: str = ""

    async def _connect(self) -> None:
        """Initialize Matrix client and verify credentials."""
        self._homeserver_url = self._credentials.get("homeserver_url", "").rstrip("/")
        self._user_id = self._credentials.get("user_id", "")
        self._access_token = self._credentials.get("access_token", "")

        if not self._homeserver_url:
            msg = "Matrix adapter requires homeserver_url credential"
            raise ValueError(msg)
        if not self._access_token:
            msg = "Matrix adapter requires access_token credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=self._homeserver_url,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=httpx.Timeout(30.0, read=60.0),
        )

        # Verify credentials
        resp = await self._client.get("/_matrix/client/v3/account/whoami")
        resp.raise_for_status()
        data = resp.json()
        self._user_id = data.get("user_id", self._user_id)

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """Sync loop for receiving Matrix events."""
        if self._client is None:
            return

        # Initial sync to get the next_batch token
        if not self._next_batch:
            resp = await self._client.get(
                "/_matrix/client/v3/sync",
                params={"timeout": "0", "filter": '{"room":{"timeline":{"limit":0}}}'},
            )
            resp.raise_for_status()
            data = resp.json()
            self._next_batch = data.get("next_batch", "")

        # Long-poll sync loop
        while not self._stop_event.is_set():
            try:
                params: dict[str, str] = {
                    "timeout": "30000",
                    "since": self._next_batch,
                }

                resp = await self._client.get("/_matrix/client/v3/sync", params=params)
                resp.raise_for_status()
                data = resp.json()

                self._next_batch = data.get("next_batch", self._next_batch)

                # Process room events
                rooms = data.get("rooms", {}).get("join", {})
                for room_id, room_data in rooms.items():
                    for event in room_data.get("timeline", {}).get("events", []):
                        await self._handle_event(room_id, event)

            except httpx.ReadTimeout:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "matrix adapter: sync error",
                    extra={"extra_data": {"account_id": self.account_id}},
                )
                await asyncio.sleep(5)

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message to a Matrix room."""
        if self._client is None:
            return None

        import uuid

        txn_id = uuid.uuid4().hex

        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": message.content,
        }

        # Add formatted body for markdown
        if self.capabilities.supports_markdown:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = message.content  # Could convert MD→HTML

        # Thread support
        if message.thread_id:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": message.thread_id,
            }
        elif message.reply_to_id:
            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": message.reply_to_id},
            }

        resp = await self._client.put(
            f"/_matrix/client/v3/rooms/{message.chat_id}/send/m.room.message/{txn_id}",
            json=content,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("event_id")

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.put(
                f"/_matrix/client/v3/rooms/{chat_id}/typing/{self._user_id}",
                json={"typing": True, "timeout": 10000},
            )

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Send read receipt."""
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                f"/_matrix/client/v3/rooms/{chat_id}/receipt/m.read/{message_id}",
                json={},
            )

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _handle_event(self, room_id: str, event: dict[str, Any]) -> None:
        """Process a Matrix room event."""
        if event.get("type") != "m.room.message":
            return

        sender = event.get("sender", "")
        # Skip own messages
        if sender == self._user_id:
            return

        content = event.get("content", {})
        body = content.get("body", "")
        if not body:
            return

        event_id = event.get("event_id", "")
        timestamp = event.get("origin_server_ts", 0)

        # Determine chat type (heuristic: DM rooms have 2 members)
        chat_type = "group"  # Default to group; could check room membership

        # Check for mention
        was_mentioned = self._user_id in body

        # Parse media
        media: list[MediaAttachment] = []
        msgtype = content.get("msgtype", "")
        if msgtype in {"m.image", "m.video", "m.audio", "m.file"}:
            media.append(
                MediaAttachment(
                    url=content.get("url"),
                    mime_type=content.get("info", {}).get("mimetype"),
                    filename=content.get("body"),
                    size_bytes=content.get("info", {}).get("size"),
                )
            )

        # Reply context
        reply_to_id = None
        relates_to = content.get("m.relates_to", {})
        in_reply_to = relates_to.get("m.in_reply_to", {})
        if in_reply_to:
            reply_to_id = in_reply_to.get("event_id")

        # Thread context
        thread_id = None
        if relates_to.get("rel_type") == "m.thread":
            thread_id = relates_to.get("event_id")

        message = InboundMessage(
            channel_type="matrix",
            account_id=self.account_id,
            message_id=event_id,
            sender_id=sender,
            sender_name=sender.split(":")[0].lstrip("@") if ":" in sender else sender,
            chat_id=room_id,
            chat_type=chat_type,
            content=body,
            reply_to_id=reply_to_id,
            thread_id=thread_id,
            media=media,
            was_mentioned=was_mentioned,
            timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC)
            if timestamp
            else datetime.now(UTC),
            platform_data={"event": event},
        )

        await self._dispatch_inbound(message)
