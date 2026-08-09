"""Google Chat adapter via Chat API.

Uses the Google Chat API for sending messages and receives inbound
messages via HTTP webhook (push subscription).

Required credentials:
- service_account_json: Google Cloud service account credentials
- project_id: Google Cloud project ID
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from cognis.channels.markdown_rendering import markdown_to_chat_text
from cognis.channels.protocol import BaseChannelAdapter, NonRetryableChannelError
from cognis.channels.registry import GOOGLE_CHAT_META
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelCapabilities,
    ChannelRecipient,
    InboundMessage,
    OutboundMessage,
    ResolvedChannelTarget,
)

logger = get_logger(__name__)

_CHAT_API_BASE = "https://chat.googleapis.com/v1"
_CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
_CHAT_SPACES_SCOPE = "https://www.googleapis.com/auth/chat.spaces.create"
_GOOGLE_USER_RESOURCE = re.compile(r"^users/[A-Za-z0-9._~:-]+$")


class GoogleChatRecipientError(NonRetryableChannelError):
    """PII-safe recipient resolution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        side_effect_certainty: str = "none",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.side_effect_certainty = side_effect_certainty


def _google_recipient_capabilities(*, supports_creation: bool) -> ChannelCapabilities:
    capabilities = GOOGLE_CHAT_META.capabilities.model_copy(deep=True)
    capabilities.recipient_capabilities.address_kinds = [
        "google_chat_space",
        "google_workspace_user",
    ]
    capabilities.recipient_capabilities.supports_resolution = True
    capabilities.recipient_capabilities.supports_creation = supports_creation
    return capabilities


def _response_name(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    name = data.get("name") if isinstance(data, Mapping) else None
    return name if isinstance(name, str) and name else None


class GoogleChatAdapter(BaseChannelAdapter):
    """Google Chat adapter via Chat API."""

    channel_type = "google_chat"
    capabilities: ChannelCapabilities = _google_recipient_capabilities(supports_creation=False)

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._service_account: dict[str, Any] = {}
        self._access_token: str = ""
        self._bot_name: str = ""
        self._app_credentials: service_account.Credentials | None = None
        self._setup_credentials: service_account.Credentials | None = None
        self._app_auth_lock = asyncio.Lock()
        self._setup_auth_lock = asyncio.Lock()

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

        self._app_credentials = service_account.Credentials.from_service_account_info(
            self._service_account,
            scopes=[_CHAT_BOT_SCOPE],
        )
        self._setup_credentials = None
        delegated_user = self._credentials.get("delegated_user") or (
            self._config.settings.get("delegated_user", "") if self._config else ""
        )
        if delegated_user:
            self._setup_credentials = service_account.Credentials.from_service_account_info(
                self._service_account,
                scopes=[_CHAT_SPACES_SCOPE],
            ).with_subject(delegated_user)
        self.capabilities = _google_recipient_capabilities(
            supports_creation=self._setup_credentials is not None
        )
        self._client = httpx.AsyncClient(
            base_url=_CHAT_API_BASE,
            timeout=30.0,
        )
        await self._get_access_token(self._app_credentials, self._app_auth_lock)

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_access_token(
        self,
        credentials: service_account.Credentials | None,
        lock: asyncio.Lock,
    ) -> str:
        """Refresh a Google-auth credential under its identity lock."""
        if credentials is None:
            raise GoogleChatRecipientError(
                "auth_unavailable", "Google Chat authorization is unavailable"
            )
        async with lock:
            if not credentials.valid or credentials.expired:
                await asyncio.to_thread(credentials.refresh, Request())
            token = credentials.token
            if not token:
                raise GoogleChatRecipientError(
                    "auth_unavailable", "Google Chat authorization is unavailable"
                )
            if credentials is self._app_credentials:
                self._access_token = token
            return token

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        credentials: service_account.Credentials | None = None,
        auth_lock: asyncio.Lock | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        if self._client is None:
            raise GoogleChatRecipientError(
                "account_unavailable", "Google Chat account is unavailable"
            )
        if credentials is not None:
            if auth_lock is None:
                raise GoogleChatRecipientError(
                    "auth_unavailable", "Google Chat authorization is unavailable"
                )
            try:
                token = await self._get_access_token(credentials, auth_lock)
            except GoogleChatRecipientError:
                raise
            except Exception as exc:
                raise GoogleChatRecipientError(
                    "auth_unavailable", "Google Chat authorization is unavailable"
                ) from exc
            headers = dict(kwargs.pop("headers", {}))
            headers["Authorization"] = f"Bearer {token}"
            kwargs["headers"] = headers
        try:
            return await self._client.request(method, path, **kwargs)
        except GoogleChatRecipientError:
            raise
        except Exception as exc:
            raise GoogleChatRecipientError(
                "resolution_unavailable", "Google Chat recipient service is unavailable"
            ) from exc

    def _target(self, chat_id: str, chat_kind: str) -> ResolvedChannelTarget:
        return ResolvedChannelTarget(
            channel_type=self.channel_type,
            account_id=self.account_id,
            chat_id=chat_id,
            chat_kind=chat_kind,  # type: ignore[arg-type]
        )

    async def resolve_recipient(
        self,
        recipient: ChannelRecipient,
        *,
        resolution_key: str,
    ) -> ResolvedChannelTarget:
        """Resolve canonical spaces or a Workspace user to a direct-message space."""
        if recipient.channel_type != self.channel_type:
            raise GoogleChatRecipientError("channel_mismatch", "Recipient channel is unsupported")
        if recipient.address_kind == "google_chat_space":
            return self._target(recipient.address, recipient.chat_kind or "direct")
        if recipient.address_kind != "google_workspace_user" or not _GOOGLE_USER_RESOURCE.fullmatch(
            recipient.address
        ):
            raise GoogleChatRecipientError(
                "unsupported_address",
                "Google Chat requires a canonical Workspace user resource",
            )
        if not (recipient.allow_resolution or recipient.allow_creation):
            raise GoogleChatRecipientError(
                "resolution_not_authorized",
                "Recipient resolution is not authorized",
            )

        if recipient.allow_resolution:
            response = await self._api_request(
                "GET",
                "/spaces:findDirectMessage",
                credentials=self._app_credentials,
                auth_lock=self._app_auth_lock,
                params={"name": recipient.address},
            )
            if response.status_code == 404:
                if not recipient.allow_creation:
                    raise GoogleChatRecipientError(
                        "recipient_not_found",
                        "Google Chat recipient was not found",
                    )
            else:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise GoogleChatRecipientError(
                        "resolution_failed", "Google Chat recipient lookup failed"
                    ) from exc
                space_name = _response_name(response)
                if space_name:
                    return self._target(space_name, "direct")
                raise GoogleChatRecipientError(
                    "invalid_response", "Google Chat returned an invalid recipient"
                )

        if not recipient.allow_creation:
            raise GoogleChatRecipientError(
                "recipient_not_found", "Google Chat recipient was not found"
            )
        if self._setup_credentials is None:
            raise GoogleChatRecipientError(
                "creation_unsupported",
                "Google Chat direct-message creation is unavailable",
            )
        response = await self._api_request(
            "POST",
            "/spaces:setup",
            credentials=self._setup_credentials,
            auth_lock=self._setup_auth_lock,
            json={
                "space": {
                    "spaceType": "DIRECT_MESSAGE",
                },
                "memberships": [{"member": {"name": recipient.address}}],
                "requestId": resolution_key,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            certainty = "none" if response.status_code in {400, 401, 403, 404, 405} else "uncertain"
            code = (
                "creation_unsupported" if response.status_code in {404, 405} else "creation_failed"
            )
            raise GoogleChatRecipientError(
                code,
                "Google Chat direct-message creation failed",
                side_effect_certainty=certainty,
            ) from exc
        space_name = _response_name(response)
        if not space_name:
            raise GoogleChatRecipientError(
                "invalid_response",
                "Google Chat returned an invalid space",
                side_effect_certainty="uncertain",
            )
        return self._target(space_name, "direct")

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

        resp = await self._api_request(
            "POST",
            f"/{message.chat_id}/messages",
            credentials=self._app_credentials,
            auth_lock=self._app_auth_lock,
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
