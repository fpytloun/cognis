"""Slack adapter via Slack API.

Supports Socket Mode (preferred, no public URL needed) and HTTP Events
API (requires public URL).  Uses the Slack Web API for sending messages.

Required credentials:
- bot_token: Bot User OAuth Token (xoxb-...)
- app_token: App-Level Token for Socket Mode (xapp-...)
- signing_secret: Signing secret for HTTP webhook verification (optional)
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import SLACK_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)

_SLACK_API_BASE = "https://slack.com/api"


class SlackAdapter(BaseChannelAdapter):
    """Slack adapter using Web API for sending and Socket Mode / Events API for receiving."""

    channel_type = "slack"
    capabilities: ChannelCapabilities = SLACK_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._bot_token: str = ""
        self._app_token: str = ""
        self._signing_secret: str = ""
        self._bot_user_id: str = ""
        self._use_socket_mode: bool = True

    async def _connect(self) -> None:
        """Initialize Slack API client."""
        self._bot_token = self._credentials.get("bot_token", "")
        self._app_token = self._credentials.get("app_token", "")
        self._signing_secret = self._credentials.get("signing_secret", "")
        self._use_socket_mode = self._credentials.get("use_socket_mode", "true").lower() in {
            "true",
            "1",
            "yes",
        }

        if not self._bot_token:
            msg = "Slack adapter requires bot_token credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=_SLACK_API_BASE,
            headers={"Authorization": f"Bearer {self._bot_token}"},
            timeout=30.0,
        )

        # Verify token and get bot info
        resp = await self._client.post("/auth.test")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            msg = f"Slack auth.test failed: {data.get('error')}"
            raise ValueError(msg)
        self._bot_user_id = data.get("user_id", "")

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """Run the Slack event listener.

        For MVP, uses a simplified polling approach.  A production
        implementation would use Socket Mode (WebSocket) via the
        slack-bolt library or the Slack Events API.
        """
        # Socket Mode requires the slack_sdk library.
        # For now, we use webhook-based events (handled via handle_webhook_payload)
        # and just keep the adapter alive.
        await self._stop_event.wait()

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via Slack Web API."""
        if self._client is None:
            return None

        payload: dict[str, Any] = {
            "channel": message.chat_id,
            "text": message.content,
        }

        if message.thread_id:
            payload["thread_ts"] = message.thread_id

        if message.reply_to_id:
            payload["thread_ts"] = message.reply_to_id

        resp = await self._client.post("/chat.postMessage", json=payload)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("ok"):
            logger.warning(
                "slack adapter: send failed",
                extra={
                    "extra_data": {
                        "account_id": self.account_id,
                        "error": data.get("error"),
                    }
                },
            )
            return None

        return data.get("ts")

    async def send_typing(self, chat_id: str) -> None:
        """Slack doesn't have a direct typing indicator API for bots."""

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Mark a channel as read up to a message."""
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                "/conversations.mark",
                json={"channel": chat_id, "ts": message_id},
            )

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify Slack request signature."""
        signing_secret = secret or self._signing_secret
        if not signing_secret:
            return False

        timestamp = headers.get("x-slack-request-timestamp", "")
        signature = headers.get("x-slack-signature", "")

        if not timestamp or not signature:
            return False

        # Check timestamp freshness (5 minutes)
        try:
            ts = int(timestamp)
            if abs(time.time() - ts) > 300:
                return False
        except ValueError:
            return False

        # Compute expected signature
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected = (
            "v0="
            + hmac.new(
                signing_secret.encode(),
                sig_basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(signature, expected)

    async def handle_webhook_payload(self, body: bytes) -> dict[str, Any] | None:
        """Process a Slack Events API payload."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None

        # Handle URL verification challenge
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge")}

        # Handle event callbacks
        if data.get("type") == "event_callback":
            event = data.get("event", {})
            if event.get("type") == "message":
                await self._handle_message_event(event)

        return {"ok": True}

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        """Process a Slack message event."""
        # Skip bot messages and message changes
        if event.get("subtype") in {"bot_message", "message_changed", "message_deleted"}:
            return
        if event.get("bot_id"):
            return
        # Skip own messages
        if event.get("user") == self._bot_user_id:
            return

        user_id = event.get("user", "")
        text = event.get("text", "")
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")

        if not text:
            return

        # Determine chat type
        channel_type = event.get("channel_type", "")
        chat_type = "direct" if channel_type == "im" else "group"

        # Check for bot mention
        was_mentioned = False
        if self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            was_mentioned = True
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()

        # Resolve user name (best effort)
        sender_name = await self._resolve_user_name(user_id)

        # Parse attachments
        media: list[MediaAttachment] = []
        for file_info in event.get("files", []):
            media.append(
                MediaAttachment(
                    url=file_info.get("url_private"),
                    filename=file_info.get("name"),
                    mime_type=file_info.get("mimetype"),
                    size_bytes=file_info.get("size"),
                )
            )

        # Parse timestamp
        try:
            timestamp = datetime.fromtimestamp(float(ts), tz=UTC)
        except (ValueError, TypeError):
            timestamp = datetime.now(UTC)

        message = InboundMessage(
            channel_type="slack",
            account_id=self.account_id,
            message_id=ts,
            sender_id=user_id,
            sender_name=sender_name,
            chat_id=channel_id,
            chat_type=chat_type,
            content=text,
            thread_id=thread_ts,
            media=media,
            was_mentioned=was_mentioned,
            timestamp=timestamp,
            platform_data={"event": event},
        )

        await self._dispatch_inbound(message)

    async def _resolve_user_name(self, user_id: str) -> str | None:
        """Resolve a Slack user ID to a display name."""
        if self._client is None:
            return None
        try:
            resp = await self._client.get(
                "/users.info",
                params={"user": user_id},
            )
            data = resp.json()
            if data.get("ok"):
                user = data.get("user", {})
                return user.get("real_name") or user.get("name")
        except Exception:
            pass
        return None
