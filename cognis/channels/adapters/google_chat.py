"""Google Chat adapter via Chat API.

Uses the Google Chat API for sending messages and receives inbound
messages via HTTP webhook (push subscription).

Required credentials:
- service_account_json: Google Cloud service account credentials
- project_id: Google Cloud project ID
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.markdown_rendering import markdown_to_chat_text
from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import GOOGLE_CHAT_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    InboundMessage,
    OutboundMessage,
)

logger = get_logger(__name__)

_CHAT_API_BASE = "https://chat.googleapis.com/v1"


class GoogleChatAdapter(BaseChannelAdapter):
    """Google Chat adapter via Chat API."""

    channel_type = "google_chat"
    capabilities: ChannelCapabilities = GOOGLE_CHAT_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._service_account: dict[str, Any] = {}
        self._access_token: str = ""
        self._bot_name: str = ""

    async def _connect(self) -> None:
        """Initialize Google Chat API client."""
        sa_json = self._credentials.get("service_account_json", "")
        if not sa_json:
            msg = "Google Chat adapter requires service_account_json credential"
            raise ValueError(msg)

        try:
            self._service_account = json.loads(sa_json)
        except json.JSONDecodeError as exc:
            msg = "Invalid service account JSON"
            raise ValueError(msg) from exc

        # For MVP, use a pre-generated access token or service account key
        # A production implementation would use google-auth library for
        # JWT-based service account authentication
        self._access_token = self._credentials.get("access_token", "")

        self._client = httpx.AsyncClient(
            base_url=_CHAT_API_BASE,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """Google Chat uses webhooks — no long-running connection needed."""
        await self._stop_event.wait()

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via Google Chat API."""
        if self._client is None:
            return None

        payload: dict[str, Any] = {
            "text": markdown_to_chat_text(message.content),
        }

        # Add image cards for media attachments
        if message.media:
            cards: list[dict[str, Any]] = []
            for media in message.media:
                if media.url and (media.mime_type or "").startswith("image/"):
                    cards.append(
                        {
                            "sections": [
                                {
                                    "widgets": [
                                        {
                                            "image": {
                                                "imageUrl": media.url,
                                                "altText": media.filename or "image",
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    )
                elif media.url:
                    payload["text"] = (
                        f"{payload.get('text', '')}\n{media.filename or 'attachment'}: {media.url}"
                    )
            if cards:
                payload["cards"] = cards

        if message.thread_id:
            payload["thread"] = {"name": message.thread_id}
            params = {"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"}
        else:
            params = {}

        resp = await self._client.post(
            f"/{message.chat_id}/messages",
            json=payload,
            params=params,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("name")

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify Google Chat webhook.

        Google Chat uses a bearer token in the Authorization header.
        The token should match the configured project's token.
        """
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        # FIXME(security): Google Chat sends a Google-signed JWT that should
        # be verified using Google's public keys. For MVP, we verify against
        # a configured secret. Replace with proper JWT verification before
        # production deployment.
        return bool(token and secret and token == secret)

    async def handle_webhook_payload(self, body: bytes) -> dict[str, Any] | None:
        """Process a Google Chat webhook event."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None

        event_type = data.get("type", "")

        if event_type == "MESSAGE":
            await self._handle_message(data)
        elif event_type == "ADDED_TO_SPACE":
            logger.info(
                "google chat adapter: added to space",
                extra={"extra_data": {"account_id": self.account_id}},
            )

        return {"text": ""}  # Google Chat expects a response

    async def _handle_message(self, data: dict[str, Any]) -> None:
        """Process a Google Chat message event."""
        msg = data.get("message", {})
        sender = msg.get("sender", {})
        space = data.get("space", {})

        text = msg.get("argumentText", "") or msg.get("text", "")
        if not text:
            return

        sender_name = sender.get("displayName", "")
        sender_id = sender.get("name", "")

        space_name = space.get("name", "")
        space_type = space.get("type", "")
        chat_type = "direct" if space_type == "DM" else "group"

        # Thread context
        thread = msg.get("thread", {})
        thread_id = thread.get("name") if thread else None

        message = InboundMessage(
            channel_type="google_chat",
            account_id=self.account_id,
            message_id=msg.get("name", ""),
            sender_id=sender_id,
            sender_name=sender_name,
            chat_id=space_name,
            chat_type=chat_type,
            chat_name=space.get("displayName"),
            content=text.strip(),
            thread_id=thread_id,
            was_mentioned=True,  # Google Chat always sends to bot when mentioned
            timestamp=datetime.now(UTC),
            platform_data={"event": data},
        )

        await self._dispatch_inbound(message)
