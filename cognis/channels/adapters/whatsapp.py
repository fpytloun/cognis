"""WhatsApp adapter via WhatsApp Business Cloud API.

Uses the Meta Graph API for sending messages and receives inbound
messages via webhook.  Requires a Meta Business account with WhatsApp
Business API access.

Required credentials:
- access_token: Permanent access token from Meta Business
- phone_number_id: WhatsApp Business phone number ID
- verify_token: Token for webhook URL verification
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from cognis.channels.markdown_rendering import markdown_to_chat_text
from cognis.channels.protocol import BaseChannelAdapter, NonRetryableChannelError
from cognis.channels.registry import WHATSAPP_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    ChannelRecipient,
    ChannelRecipientCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
    ResolvedChannelTarget,
)

logger = get_logger(__name__)

_GRAPH_API_BASE = "https://graph.facebook.com"
_WHATSAPP_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_WHATSAPP_POLICY_CODES = {
    131026,  # Undeliverable message
    131047,  # Re-engagement message outside the service window
    132000,  # Template parameter count mismatch
    132001,  # Template does not exist
    132012,  # Template parameter format mismatch
}


class _WhatsAppProviderError(Exception):
    """PII-safe WhatsApp provider failure."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _WhatsAppRecipientError(_WhatsAppProviderError, NonRetryableChannelError):
    """A permanent local recipient validation failure."""


class _WhatsAppPermanentError(_WhatsAppProviderError, NonRetryableChannelError):
    """A confirmed permanent WhatsApp provider failure."""


def _whatsapp_graph_error(resp: httpx.Response) -> _WhatsAppProviderError:
    """Classify a Graph error without copying provider text or recipient data."""
    provider_code: int | None = None
    try:
        payload = resp.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            raw_code = error.get("code")
            if isinstance(raw_code, int):
                provider_code = raw_code
            elif isinstance(raw_code, str) and raw_code.isdigit():
                provider_code = int(raw_code)
    except (TypeError, ValueError):
        pass

    if provider_code == 190:
        return _WhatsAppPermanentError(
            "provider_auth", "WhatsApp provider authentication was rejected"
        )
    if provider_code in _WHATSAPP_POLICY_CODES:
        return _WhatsAppPermanentError(
            "provider_policy", "WhatsApp provider policy rejected the message"
        )
    if resp.status_code >= 500:
        return _WhatsAppProviderError(
            "provider_error",
            "WhatsApp provider request failed",
            retryable=True,
        )
    return _WhatsAppProviderError(
        "provider_unknown",
        "WhatsApp provider returned an unclassified error",
    )


class WhatsAppAdapter(BaseChannelAdapter):
    """WhatsApp Business Cloud API adapter."""

    channel_type = "whatsapp"
    capabilities: ChannelCapabilities = WHATSAPP_META.capabilities.model_copy(
        update={
            "recipient_capabilities": ChannelRecipientCapabilities(
                address_kinds=["whatsapp_e164"],
                chat_kinds=["direct"],
                supports_resolution=False,
                supports_creation=False,
            )
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = ""
        self._phone_number_id: str = ""
        self._verify_token: str = ""
        self._api_version: str = "v21.0"

    async def resolve_recipient(
        self,
        recipient: ChannelRecipient,
        *,
        resolution_key: str,
    ) -> ResolvedChannelTarget:
        """Validate an E.164 address and return the provider's numeric chat ID."""
        del resolution_key
        if recipient.channel_type != self.channel_type:
            raise _WhatsAppRecipientError(
                "channel_mismatch", "Recipient channel does not match this account"
            )
        if recipient.allow_creation:
            raise _WhatsAppRecipientError(
                "creation_unsupported", "WhatsApp recipient creation is not supported"
            )
        if recipient.allow_resolution:
            raise _WhatsAppRecipientError(
                "resolution_unsupported", "WhatsApp recipient resolution is not supported"
            )
        if recipient.address_kind != "whatsapp_e164":
            raise _WhatsAppRecipientError(
                "unsupported_address_kind",
                "WhatsApp recipient address kind is not supported",
            )
        if recipient.chat_kind not in {None, "direct"}:
            raise _WhatsAppRecipientError(
                "chat_kind_mismatch", "WhatsApp recipient chat kind does not match its address"
            )
        address = recipient.address.strip()
        if not _WHATSAPP_E164.fullmatch(address):
            raise _WhatsAppRecipientError(
                "invalid_address", "WhatsApp recipient address is invalid"
            )
        return ResolvedChannelTarget(
            channel_type=self.channel_type,
            account_id=self.account_id,
            chat_id=address[1:],
            chat_kind="direct",
        )

    async def _connect(self) -> None:
        """Initialize HTTP client for WhatsApp Cloud API."""
        self._access_token = self._credentials.get("access_token", "")
        self._phone_number_id = self._credentials.get("phone_number_id", "")
        self._verify_token = self._credentials.get("verify_token", "")
        self._api_version = self._credentials.get("api_version", "v21.0")

        if not self._access_token:
            msg = "WhatsApp adapter requires access_token credential"
            raise ValueError(msg)
        if not self._phone_number_id:
            msg = "WhatsApp adapter requires phone_number_id credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=f"{_GRAPH_API_BASE}/{self._api_version}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30.0,
        )

        # Verify connectivity
        resp = await self._client.get(f"/{self._phone_number_id}")
        resp.raise_for_status()

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        """WhatsApp uses webhooks — no long-running connection needed.

        This method blocks until stop is requested.
        """
        await self._stop_event.wait()

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via WhatsApp Cloud API."""
        if self._client is None:
            return None

        # Send media attachments
        for media in message.media:
            await self._send_media(message.chat_id, media, reply_to=message.reply_to_id)

        if not message.content.strip() and message.media:
            return None

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": message.chat_id,
            "type": "text",
            "text": {"body": markdown_to_chat_text(message.content)},
        }

        if message.reply_to_id:
            payload["context"] = {"message_id": message.reply_to_id}

        resp = await self._client.post(
            f"/{self._phone_number_id}/messages",
            json=payload,
        )
        if resp.is_error:
            raise _whatsapp_graph_error(resp)
        result = resp.json()
        if isinstance(result, dict) and isinstance(result.get("error"), dict):
            raise _whatsapp_graph_error(resp)
        messages = result.get("messages", [])
        return messages[0].get("id") if messages else None

    async def _send_media(
        self, chat_id: str, media: MediaAttachment, *, reply_to: str | None = None
    ) -> None:
        if self._client is None or not media.url:
            return
        try:
            mime = media.mime_type or "application/octet-stream"
            if mime.startswith("image/"):
                media_type = "image"
            elif mime.startswith("video/"):
                media_type = "video"
            elif mime.startswith("audio/"):
                media_type = "audio"
            else:
                media_type = "document"
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "to": chat_id,
                "type": media_type,
                media_type: {"link": media.url},
            }
            if media.filename and media_type == "document":
                payload[media_type]["filename"] = media.filename
            if reply_to:
                payload["context"] = {"message_id": reply_to}
            resp = await self._client.post(f"/{self._phone_number_id}/messages", json=payload)
            if resp.is_error:
                raise _whatsapp_graph_error(resp)
        except (_WhatsAppRecipientError, _WhatsAppPermanentError):
            raise
        except Exception:
            logger.warning(
                "whatsapp adapter: media send failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Mark a message as read."""
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.post(
                f"/{self._phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                },
            )

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify WhatsApp webhook signature (X-Hub-Signature-256)."""
        signature = headers.get("x-hub-signature-256", "")
        if not signature or not secret:
            return False

        expected = (
            "sha256="
            + hmac.new(
                secret.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(signature, expected)

    async def handle_webhook_payload(self, body: bytes) -> dict[str, Any] | None:
        """Process a WhatsApp webhook payload."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None

        # Handle webhook verification challenge
        if "hub.mode" in str(data):
            return None  # Handled at the route level

        # Process message entries
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if value.get("messaging_product") != "whatsapp":
                    continue

                for msg in value.get("messages", []):
                    await self._handle_message(msg, value.get("contacts", []))

        return {"status": "ok"}

    async def _handle_message(
        self,
        msg: dict[str, Any],
        contacts: list[dict[str, Any]],
    ) -> None:
        """Process a single WhatsApp message."""
        msg_type = msg.get("type", "")
        msg_id = msg.get("id", "")
        sender = msg.get("from", "")
        timestamp = msg.get("timestamp", "")

        # Extract sender name from contacts
        sender_name = None
        for contact in contacts:
            if contact.get("wa_id") == sender:
                profile = contact.get("profile", {})
                sender_name = profile.get("name")
                break

        # Extract content based on message type
        content = ""
        media: list[MediaAttachment] = []

        if msg_type == "text":
            content = msg.get("text", {}).get("body", "")
        elif msg_type in {"image", "video", "audio", "document"}:
            media_data = msg.get(msg_type, {})
            content = media_data.get("caption", "")
            media.append(
                MediaAttachment(
                    platform_id=media_data.get("id"),
                    mime_type=media_data.get("mime_type"),
                    filename=media_data.get("filename"),
                )
            )
        elif msg_type == "sticker":
            content = "[sticker]"
        elif msg_type == "location":
            loc = msg.get("location", {})
            content = f"Location: {loc.get('latitude')}, {loc.get('longitude')}"
        else:
            return  # Unsupported message type

        if not content and not media:
            return

        # Determine reply context
        reply_to_id = None
        context = msg.get("context")
        if context:
            reply_to_id = context.get("id")

        message = InboundMessage(
            channel_type="whatsapp",
            account_id=self.account_id,
            message_id=msg_id,
            sender_id=sender,
            sender_name=sender_name,
            chat_id=sender,  # WhatsApp DMs use sender as chat ID
            chat_type="direct",
            content=content,
            reply_to_id=reply_to_id,
            media=media,
            timestamp=datetime.fromtimestamp(int(timestamp), tz=UTC)
            if timestamp
            else datetime.now(UTC),
            platform_data={"raw_message": msg},
        )
        if msg_type == "audio" and bool(msg.get("audio", {}).get("voice")):
            message.platform_data["voice_input"] = True

        await self._dispatch_inbound(message)

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        if self._client is None or not attachment.platform_id:
            return None
        resp = await self._client.get(f"/{attachment.platform_id}")
        resp.raise_for_status()
        media_meta = resp.json()
        media_url = media_meta.get("url")
        if not isinstance(media_url, str) or not media_url:
            return None
        download = await self._client.get(media_url)
        download.raise_for_status()
        return (
            download.content,
            attachment.mime_type
            or download.headers.get("content-type", "application/octet-stream"),
            attachment.filename or f"{attachment.platform_id}",
        )
