"""Matrix adapter via HTTP Client-Server API.

This adapter intentionally avoids mandatory Matrix SDK/E2EE dependencies.
It supports production-grade unencrypted Matrix rooms through the Matrix
Client-Server API and fails honestly for encrypted media it cannot decrypt.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import re
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from cognis.channels.group_context import group_context_policy
from cognis.channels.markdown_rendering import markdown_to_plain_text
from cognis.channels.matrix_formatting import markdown_to_matrix_html
from cognis.channels.protocol import BaseChannelAdapter, NonRetryableChannelError
from cognis.channels.registry import MATRIX_META
from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelCapabilities,
    ChannelRecipient,
    ChannelRecipientCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
    ResolvedChannelTarget,
)

logger = get_logger(__name__)

_SEEN_EVENT_LIMIT = 4096
_MAX_MEDIA_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MATRIX_HTML_FORMAT = "org.matrix.custom.html"


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _split_setting(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value).split(",") if part.strip()}


def _event_content(event: dict[str, Any]) -> dict[str, Any]:
    content = event.get("content")
    return content if isinstance(content, dict) else {}


def _relates_to(content: dict[str, Any]) -> dict[str, Any]:
    relates = content.get("m.relates_to")
    return relates if isinstance(relates, dict) else {}


def _strip_matrix_reply_fallback(body: str) -> str:
    """Remove Matrix reply fallback from a plain-text message body."""

    if not body.startswith("> "):
        return body
    lines = body.splitlines()
    while lines and lines[0].startswith("> "):
        lines.pop(0)
    if lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()


def _strip_html_tags(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value))


def _contains_phrase_mention(haystack: str, phrase: str) -> bool:
    if not phrase.strip():
        return False
    pattern = rf"(?<![\w@]){re.escape(phrase)}(?![\w.-])"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def _contains_localpart_mention(haystack: str, localpart: str) -> bool:
    if not localpart.strip():
        return False
    pattern = rf"(?<![\w])@?{re.escape(localpart)}(?![\w.-])"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def _markdown_to_matrix_html(value: str, *, compact: bool = False) -> str:
    """Convert Markdown to Matrix-compatible HTML."""

    return markdown_to_matrix_html(value, compact=compact)


def _matrix_msgtype_for_mime(mime: str) -> str:
    if mime.startswith("image/"):
        return "m.image"
    if mime.startswith("video/"):
        return "m.video"
    if mime.startswith("audio/"):
        return "m.audio"
    return "m.file"


def _is_matrix_voice_message(content: dict[str, Any]) -> bool:
    """Return true for Matrix voice-note events, not generic audio files."""

    if content.get("msgtype") != "m.audio":
        return False
    voice = content.get("org.matrix.msc3245.voice")
    return isinstance(voice, dict)


def _is_safe_download_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.hostname:
        return False
    return not (parsed.username or parsed.password)


class MatrixRecipientResolutionError(NonRetryableChannelError):
    """Safe, stable error raised while resolving a Matrix recipient."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        side_effect_certainty: str = "none",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.side_effect_certainty = side_effect_certainty


_MATRIX_CAPABILITIES = MATRIX_META.capabilities.model_copy(deep=True)
_MATRIX_CAPABILITIES.recipient_capabilities = ChannelRecipientCapabilities(
    address_kinds=["matrix_room_id", "matrix_room_alias", "matrix_user_id"],
    chat_kinds=["direct", "group"],
    supports_resolution=True,
    supports_creation=True,
)


class MatrixAdapter(BaseChannelAdapter):
    """Matrix protocol adapter via Client-Server API."""

    channel_type = "matrix"
    capabilities: ChannelCapabilities = _MATRIX_CAPABILITIES

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._homeserver_url: str = ""
        self._user_id: str = ""
        self._access_token: str = ""
        self._next_batch: str = ""
        self._display_name: str = ""
        self._allow_rooms: set[str] = set()
        self._direct_rooms: set[str] = set()
        self._group_rooms: set[str] = set()
        self._auto_join_invites = False
        self._require_mention = False
        self._group_context_enabled = False
        self._ignore_appservice_senders = True
        self._ignored_senders: set[str] = set()
        self._ignored_sender_patterns: list[re.Pattern[str]] = []
        self._suppress_startup_replay = True
        self._live_sync_established = False
        self._started_at_ms = 0
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        self._room_type_cache: dict[str, str] = {}
        self._profile_display_names: OrderedDict[str, str] = OrderedDict()
        self._synced_avatar_signature: str | None = None
        self._created_direct_rooms: dict[str, str] = {}
        self._direct_resolution_attempts: set[str] = set()
        self._direct_mapping_lock = asyncio.Lock()

    async def _connect(self) -> None:
        """Initialize Matrix client and verify credentials."""
        self._homeserver_url = self._credentials.get("homeserver_url", "").rstrip("/")
        self._user_id = self._credentials.get("user_id", "")
        self._access_token = self._credentials.get("access_token", "")
        settings = self._config.settings if self._config else {}
        self._allow_rooms = _split_setting(settings.get("allowed_rooms"))
        self._direct_rooms = _split_setting(settings.get("direct_rooms"))
        self._group_rooms = _split_setting(settings.get("group_rooms"))
        self._auto_join_invites = _as_bool(settings.get("auto_join_invites"), default=False)
        self._require_mention = _as_bool(settings.get("require_mention"), default=False)
        self._group_context_enabled = group_context_policy(settings).enabled
        self._ignore_appservice_senders = _as_bool(
            settings.get("ignore_appservice_senders"),
            default=True,
        )
        self._ignored_senders = _split_setting(settings.get("ignored_senders"))
        self._ignored_sender_patterns = []
        for pattern in _split_setting(settings.get("ignored_sender_patterns")):
            try:
                self._ignored_sender_patterns.append(re.compile(pattern))
            except re.error:
                logger.warning(
                    "matrix adapter: ignored invalid sender regex",
                    extra={"extra_data": {"account_id": self.account_id, "pattern": pattern}},
                )
        self._suppress_startup_replay = _as_bool(
            settings.get("suppress_startup_replay"),
            default=True,
        )
        self._live_sync_established = False
        self._started_at_ms = int(time.time() * 1000)
        self._room_type_cache = {}

        if not self._homeserver_url:
            msg = "Matrix adapter requires homeserver_url credential"
            raise ValueError(msg)
        if not self._access_token:
            await self._login_with_password()

        self._client = httpx.AsyncClient(
            base_url=self._homeserver_url,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=httpx.Timeout(30.0, read=60.0),
        )

        # Verify credentials
        resp = await self._client.get("/_matrix/client/v3/account/whoami")
        self._raise_for_status(resp, "Matrix account verification failed")
        data = resp.json()
        self._user_id = data.get("user_id", self._user_id)
        self._display_name = await self._fetch_display_name()
        self._direct_rooms.update(await self._fetch_direct_rooms())

    async def _login_with_password(self) -> None:
        """Resolve an access token through Matrix password login when configured."""

        username = self._credentials.get("username") or self._user_id
        password = self._credentials.get("password", "")
        device_id = self._credentials.get("device_id") or None
        if not username or not password:
            msg = "Matrix adapter requires access_token or username/password credentials"
            raise ValueError(msg)
        async with httpx.AsyncClient(
            base_url=self._homeserver_url,
            timeout=httpx.Timeout(30.0, read=60.0),
        ) as client:
            payload: dict[str, Any] = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": username},
                "password": password,
            }
            if device_id:
                payload["device_id"] = device_id
            resp = await client.post("/_matrix/client/v3/login", json=payload)
            self._raise_for_status(resp, "Matrix password login failed")
            data = resp.json()
        self._access_token = data.get("access_token", "")
        self._user_id = data.get("user_id", self._user_id)
        if not self._access_token:
            msg = "Matrix password login did not return an access token"
            raise ValueError(msg)

    async def _disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _raise_for_status(self, resp: httpx.Response, context: str) -> None:
        if resp.status_code in {401, 403}:
            raise NonRetryableChannelError(f"{context}: HTTP {resp.status_code}")
        resp.raise_for_status()

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
            self._raise_for_status(resp, "Matrix initial sync failed")
            data = resp.json()
            self._next_batch = data.get("next_batch", "")
            self._live_sync_established = True

        # Long-poll sync loop
        while not self._stop_event.is_set():
            try:
                params: dict[str, str] = {
                    "timeout": "30000",
                    "since": self._next_batch,
                }

                resp = await self._client.get("/_matrix/client/v3/sync", params=params)
                self._raise_for_status(resp, "Matrix sync failed")
                data = resp.json()

                self._next_batch = data.get("next_batch", self._next_batch)

                # Process room events
                rooms_root = data.get("rooms", {})
                if self._auto_join_invites:
                    for room_id in rooms_root.get("invite", {}):
                        if self._room_is_allowed(room_id):
                            await self._join_room(room_id)

                rooms = rooms_root.get("join", {})
                for room_id, room_data in rooms.items():
                    for event in room_data.get("timeline", {}).get("events", []):
                        await self._handle_event(room_id, event, room_data=room_data)

            except httpx.ReadTimeout:
                continue
            except asyncio.CancelledError:
                raise
            except NonRetryableChannelError:
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

        # Explicit attachments remain separate Matrix events. Rich deliverable
        # illustrations use the inline disposition and are embedded in the text event.
        for media in message.media:
            if media.disposition == "attachment":
                await self._send_media(message.chat_id, media, thread_id=message.thread_id)

        inline_media = [media for media in message.media if media.disposition == "inline"]
        if not message.content.strip() and message.media and not inline_media:
            return None

        idempotency_key = message.platform_data.get("idempotency_key")
        txn_id = (
            quote(str(idempotency_key), safe="")
            if isinstance(idempotency_key, str) and idempotency_key.strip()
            else uuid.uuid4().hex
        )

        compact = message.platform_data.get("canonical_rich_markdown") is True
        rendered_content, inline_images = await self._render_inline_media(
            message.content, inline_media
        )
        plain_content = re.sub(
            r"<!--cognis-rich-media:[^:>]+:(.+?)-->",
            lambda match: match.group(1),
            message.content,
        )
        formatted = _markdown_to_matrix_html(rendered_content, compact=compact)
        for marker, image in inline_images.items():
            formatted = formatted.replace(marker, image, 1)
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": markdown_to_plain_text(plain_content),
            "format": _MATRIX_HTML_FORMAT,
            "formatted_body": formatted,
        }
        if compact:
            # MSC2385 best-effort hint. Clients that do not support it safely
            # ignore the unknown field and may still render URL previews.
            content["url_previews"] = []

        mentions = self._extract_outbound_mentions(message)
        if mentions:
            content["m.mentions"] = {"user_ids": sorted(mentions)}

        # Ordinary DM replies stay in the room timeline. Besides avoiding
        # unnecessary reply nesting, this prevents Matrix clients from losing
        # a later thread rooted on an agent reply. Genuine Matrix threads must
        # still preserve both their root and immediate reply target.
        reply_to_id = message.reply_to_id
        if self._is_direct_room(message.chat_id) and not message.thread_id:
            reply_to_id = None
        relates = self._outbound_relates_to(message.thread_id, reply_to_id)
        if relates:
            content["m.relates_to"] = relates

        resp = await self._client.put(
            f"/_matrix/client/v3/rooms/{message.chat_id}/send/m.room.message/{txn_id}",
            json=content,
        )
        self._raise_for_status(resp, "Matrix message send failed")
        result = resp.json()
        return result.get("event_id")

    async def resolve_recipient(
        self,
        recipient: ChannelRecipient,
        *,
        resolution_key: str,
    ) -> ResolvedChannelTarget:
        """Resolve a Matrix room, alias, or direct-message user."""
        if recipient.channel_type != self.channel_type:
            raise MatrixRecipientResolutionError(
                "channel_mismatch", "Recipient channel does not match this adapter"
            )
        if recipient.address_kind not in {
            "matrix_room_id",
            "matrix_room_alias",
            "matrix_user_id",
        }:
            raise MatrixRecipientResolutionError(
                "unsupported_address_kind", "Recipient address kind is unsupported"
            )
        if recipient.chat_kind not in {"direct", "group"}:
            raise MatrixRecipientResolutionError(
                "unsupported_chat_kind", "Recipient chat kind is unsupported"
            )

        if recipient.address_kind == "matrix_room_id":
            self._check_room_policy(recipient.address, recipient.chat_kind)
            return self._target(recipient.address, recipient.chat_kind)

        if recipient.address_kind == "matrix_room_alias":
            if recipient.chat_kind != "group":
                raise MatrixRecipientResolutionError(
                    "unsupported_chat_kind", "Matrix room aliases support group chats only"
                )
            if not recipient.allow_resolution:
                raise MatrixRecipientResolutionError(
                    "resolution_required", "Matrix room alias resolution requires authorization"
                )
            client = self._client
            if client is None:
                raise MatrixRecipientResolutionError(
                    "account_unavailable", "Matrix account is unavailable", retryable=True
                )
            alias = recipient.address
            try:
                response = await client.get(
                    f"/_matrix/client/v3/directory/room/{quote(alias, safe='')}"
                )
                response.raise_for_status()
                data = response.json()
                room_id = data.get("room_id") if isinstance(data, dict) else None
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                raise MatrixRecipientResolutionError(
                    "matrix_alias_resolution_failed",
                    "Matrix room alias resolution failed",
                    retryable=True,
                ) from exc
            if not isinstance(room_id, str) or not room_id:
                raise MatrixRecipientResolutionError(
                    "matrix_response_invalid", "Matrix room alias resolution returned no room"
                )
            self._check_room_policy(room_id, "group")
            await self._ensure_joined(room_id)
            return self._target(room_id, "group")

        if recipient.chat_kind != "direct":
            raise MatrixRecipientResolutionError(
                "unsupported_chat_kind", "Matrix user recipients support direct chats only"
            )
        if not (recipient.allow_resolution or recipient.allow_creation):
            raise MatrixRecipientResolutionError(
                "resolution_not_authorized", "Matrix user resolution is not authorized"
            )
        if self._client is None:
            raise MatrixRecipientResolutionError(
                "account_unavailable", "Matrix account is unavailable", retryable=True
            )

        if recipient.allow_resolution:
            direct_data = await self._fetch_direct_account_data()
            existing_room = self._direct_room_for_user(direct_data, recipient.address)
            if existing_room:
                self._direct_rooms.add(existing_room)
                return self._target(existing_room, "direct")

        known_room = self._created_direct_rooms.get(resolution_key)
        if known_room:
            await self._repair_direct_room(recipient.address, known_room)
            return self._target(known_room, "direct")
        if resolution_key in self._direct_resolution_attempts:
            reconciled = await self._reconcile_direct_room(recipient.address)
            if reconciled:
                self._created_direct_rooms[resolution_key] = reconciled
                self._direct_rooms.add(reconciled)
                return self._target(reconciled, "direct")
        if not recipient.allow_creation:
            raise MatrixRecipientResolutionError(
                "creation_required", "Matrix direct-room creation requires authorization"
            )

        self._direct_resolution_attempts.add(resolution_key)
        try:
            response = await self._client.post(
                "/_matrix/client/v3/createRoom",
                json={
                    "invite": [recipient.address],
                    "is_direct": True,
                    "preset": "trusted_private_chat",
                },
            )
            if response.status_code >= 500 or response.status_code == 429:
                raise MatrixRecipientResolutionError(
                    "matrix_creation_uncertain",
                    "Matrix direct-room creation outcome is uncertain",
                    retryable=True,
                    side_effect_certainty="uncertain",
                )
            if response.status_code >= 400:
                raise MatrixRecipientResolutionError(
                    "matrix_creation_rejected",
                    "Matrix direct-room creation was rejected",
                )
            response.raise_for_status()
            data = response.json()
            room_id = data.get("room_id") if isinstance(data, dict) else None
        except MatrixRecipientResolutionError as exc:
            if exc.side_effect_certainty == "uncertain":
                reconciled = await self._reconcile_direct_room(recipient.address)
                if reconciled:
                    self._created_direct_rooms[resolution_key] = reconciled
                    self._direct_rooms.add(reconciled)
                    return self._target(reconciled, "direct")
            raise
        except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
            reconciled = await self._reconcile_direct_room(recipient.address)
            if reconciled:
                self._created_direct_rooms[resolution_key] = reconciled
                self._direct_rooms.add(reconciled)
                return self._target(reconciled, "direct")
            raise MatrixRecipientResolutionError(
                "matrix_creation_uncertain",
                "Matrix direct-room creation outcome is uncertain",
                retryable=True,
                side_effect_certainty="uncertain",
            ) from exc
        except httpx.HTTPError as exc:
            reconciled = await self._reconcile_direct_room(recipient.address)
            if reconciled:
                self._created_direct_rooms[resolution_key] = reconciled
                self._direct_rooms.add(reconciled)
                return self._target(reconciled, "direct")
            raise MatrixRecipientResolutionError(
                "matrix_creation_uncertain",
                "Matrix direct-room creation outcome is uncertain",
                retryable=True,
                side_effect_certainty="uncertain",
            ) from exc
        except (TypeError, ValueError, AttributeError) as exc:
            raise MatrixRecipientResolutionError(
                "matrix_response_invalid", "Matrix direct-room creation returned no room"
            ) from exc
        if not isinstance(room_id, str) or not room_id:
            reconciled = await self._reconcile_direct_room(recipient.address)
            if reconciled:
                self._created_direct_rooms[resolution_key] = reconciled
                self._direct_rooms.add(reconciled)
                return self._target(reconciled, "direct")
            raise MatrixRecipientResolutionError(
                "matrix_creation_uncertain",
                "Matrix direct-room creation outcome is uncertain",
                retryable=True,
                side_effect_certainty="uncertain",
            )
        self._created_direct_rooms[resolution_key] = room_id
        await self._repair_direct_room(recipient.address, room_id)
        self._direct_rooms.add(room_id)
        return self._target(room_id, "direct")

    def _target(self, room_id: str, chat_kind: str) -> ResolvedChannelTarget:
        return ResolvedChannelTarget(
            channel_type=self.channel_type,
            account_id=self.account_id,
            chat_id=room_id,
            chat_kind=chat_kind,  # type: ignore[arg-type]
        )

    def _check_room_policy(self, room_id: str, chat_kind: str) -> None:
        if chat_kind == "group" and not self._room_is_allowed(room_id):
            raise MatrixRecipientResolutionError(
                "room_not_allowed", "Matrix group room is not allowed"
            )

    async def _ensure_joined(self, room_id: str) -> None:
        client = self._client
        if client is None:
            raise MatrixRecipientResolutionError(
                "account_unavailable", "Matrix account is unavailable", retryable=True
            )
        try:
            response = await client.get("/_matrix/client/v3/joined_rooms")
            response.raise_for_status()
            data = response.json()
            joined = data.get("joined_rooms", []) if isinstance(data, dict) else []
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise MatrixRecipientResolutionError(
                "matrix_join_state_failed",
                "Matrix room membership could not be checked",
                retryable=True,
            ) from exc
        if isinstance(joined, list) and room_id in joined:
            return
        try:
            response = await client.post(
                f"/_matrix/client/v3/join/{quote(room_id, safe='')}",
                json={},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MatrixRecipientResolutionError(
                "matrix_join_failed", "Matrix room join failed", retryable=True
            ) from exc

    async def _fetch_direct_account_data(self) -> dict[str, Any]:
        if self._client is None or not self._user_id:
            raise MatrixRecipientResolutionError(
                "account_unavailable", "Matrix account is unavailable", retryable=True
            )
        try:
            response = await self._client.get(
                f"/_matrix/client/v3/user/{self._user_id}/account_data/m.direct"
            )
        except httpx.HTTPError as exc:
            raise MatrixRecipientResolutionError(
                "matrix_account_data_failed",
                "Matrix direct-room mapping could not be read",
                retryable=True,
            ) from exc
        if response.status_code == 404:
            return {}
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MatrixRecipientResolutionError(
                "matrix_account_data_failed",
                "Matrix direct-room mapping could not be read",
                retryable=True,
            ) from exc
        if not isinstance(data, dict):
            raise MatrixRecipientResolutionError(
                "matrix_response_invalid", "Matrix direct-room mapping is invalid"
            )
        return data

    @staticmethod
    def _direct_room_for_user(data: dict[str, Any], user_id: str) -> str | None:
        rooms = data.get(user_id)
        if not isinstance(rooms, list):
            return None
        return next(
            (room for room in rooms if isinstance(room, str) and room.startswith("!")), None
        )

    async def _reconcile_direct_room(self, user_id: str) -> str | None:
        try:
            data = await self._fetch_direct_account_data()
        except Exception:
            return None
        return self._direct_room_for_user(data, user_id)

    async def _add_direct_room(self, user_id: str, room_id: str) -> None:
        async with self._direct_mapping_lock:
            data = await self._fetch_direct_account_data()
            rooms = data.get(user_id)
            if not isinstance(rooms, list):
                rooms = []
            if room_id not in rooms:
                rooms.append(room_id)
            data[user_id] = rooms
            if self._client is None:
                raise httpx.NetworkError("Matrix account is unavailable")
            response = await self._client.put(
                f"/_matrix/client/v3/user/{self._user_id}/account_data/m.direct",
                json=data,
            )
            response.raise_for_status()

    async def _repair_direct_room(self, user_id: str, room_id: str) -> None:
        try:
            await self._add_direct_room(user_id, room_id)
        except MatrixRecipientResolutionError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MatrixRecipientResolutionError(
                "matrix_account_data_failed",
                "Matrix direct-room mapping could not be saved",
                retryable=True,
            ) from exc

    async def _render_inline_media(
        self, content: str, media: list[MediaAttachment]
    ) -> tuple[str, dict[str, str]]:
        """Upload rich images and preserve their canonical document positions."""

        if self._client is None:
            return content, {}
        images: dict[str, str] = {}
        media_by_ref = {item.media_ref: item for item in media if item.media_ref}
        marker_pattern = re.compile(r"<!--cognis-rich-media:([^:>]+):(.+?)-->")
        rendered_content = content
        referenced: set[str] = set()
        for match in marker_pattern.finditer(content):
            media_ref = unquote(match.group(1))
            item = media_by_ref.get(media_ref)
            placeholder = self._inline_media_placeholder(rendered_content)
            if item is None or not (item.mime_type or "").startswith("image/"):
                rendered_content = rendered_content.replace(match.group(0), match.group(2), 1)
                continue
            images[placeholder] = await self._upload_inline_media(item, alt=match.group(2))
            referenced.add(media_ref)
            rendered_content = rendered_content.replace(match.group(0), placeholder, 1)

        for item in media:
            if item.media_ref in referenced or not (item.mime_type or "").startswith("image/"):
                continue
            placeholder = self._inline_media_placeholder(rendered_content)
            images[placeholder] = await self._upload_inline_media(
                item, alt=item.filename or "image"
            )
            rendered_content = f"{rendered_content}\n\n{placeholder}".strip()
        return rendered_content, images

    @staticmethod
    def _inline_media_placeholder(content: str) -> str:
        while True:
            placeholder = f"COGNISRICHMEDIA{uuid.uuid4().hex}"
            if placeholder not in content:
                return placeholder

    async def _upload_inline_media(self, item: MediaAttachment, *, alt: str) -> str:
        client = self._client
        if client is None:
            msg = "Matrix inline media upload requires an active client"
            raise RuntimeError(msg)
        file_content = await self._load_outbound_media(item)
        if file_content is None:
            msg = "Matrix inline media could not be loaded"
            raise RuntimeError(msg)
        filename = item.filename or "image"
        upload_resp = await client.post(
            "/_matrix/media/v3/upload",
            content=file_content,
            headers={"Content-Type": item.mime_type or "image/*"},
            params={"filename": filename},
        )
        self._raise_for_status(upload_resp, "Matrix inline media upload failed")
        mxc_url = upload_resp.json().get("content_uri")
        if not isinstance(mxc_url, str) or not mxc_url:
            msg = "Matrix inline media upload returned no MXC URL"
            raise RuntimeError(msg)
        return (
            f'<img src="{html.escape(mxc_url, quote=True)}" alt="{html.escape(alt, quote=True)}">'
        )

    async def _send_media(
        self, room_id: str, media: MediaAttachment, *, thread_id: str | None = None
    ) -> None:
        if self._client is None:
            return
        try:
            file_content = await self._load_outbound_media(media)
            if file_content is None:
                return
            mime = media.mime_type or "application/octet-stream"
            filename = media.filename or "attachment"
            upload_resp = await self._client.post(
                "/_matrix/media/v3/upload",
                content=file_content,
                headers={"Content-Type": mime},
                params={"filename": filename},
            )
            self._raise_for_status(upload_resp, "Matrix media upload failed")
            mxc_url = upload_resp.json().get("content_uri")
            if not mxc_url:
                return
            msgtype = _matrix_msgtype_for_mime(mime)
            event_content: dict[str, Any] = {
                "msgtype": msgtype,
                "body": filename,
                "url": mxc_url,
                "info": {"mimetype": mime, "size": len(file_content)},
            }
            if thread_id:
                event_content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
            txn = uuid.uuid4().hex
            send_resp = await self._client.put(
                f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
                json=event_content,
            )
            self._raise_for_status(send_resp, "Matrix media message send failed")
        except Exception:
            logger.warning(
                "matrix adapter: media send failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

    async def _load_outbound_media(self, media: MediaAttachment) -> bytes | None:
        if media.content_b64:
            try:
                return base64.b64decode(media.content_b64, validate=True)
            except ValueError:
                logger.warning(
                    "matrix adapter: invalid base64 media payload",
                    extra={"extra_data": {"account_id": self.account_id}},
                )
                return None
        if not media.url or not _is_safe_download_url(media.url):
            return None
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as dl:
            resp = await dl.get(media.url)
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > _MAX_MEDIA_DOWNLOAD_BYTES:
                        return None
                except ValueError:
                    return None
            if len(resp.content) > _MAX_MEDIA_DOWNLOAD_BYTES:
                return None
            return resp.content

    async def sync_profile(self, profile: AgentProfile) -> None:
        if self._client is None:
            return
        try:
            changed = False
            if self._display_name != profile.effective_name:
                display_name_resp = await self._client.put(
                    f"/_matrix/client/v3/profile/{self._user_id}/displayname",
                    json={"displayname": profile.effective_name},
                )
                self._raise_for_status(display_name_resp, "Matrix display name update failed")
                self._display_name = profile.effective_name
                changed = True
            if profile.avatar_bytes and profile.avatar_content_type:
                avatar_signature = hashlib.sha256(
                    profile.avatar_content_type.encode() + b"\0" + profile.avatar_bytes
                ).hexdigest()
                if self._synced_avatar_signature != avatar_signature:
                    upload_resp = await self._client.post(
                        "/_matrix/media/v3/upload",
                        content=profile.avatar_bytes,
                        headers={"Content-Type": profile.avatar_content_type},
                        params={"filename": "avatar"},
                    )
                    upload_resp.raise_for_status()
                    mxc_url = upload_resp.json().get("content_uri")
                    if mxc_url:
                        avatar_url_resp = await self._client.put(
                            f"/_matrix/client/v3/profile/{self._user_id}/avatar_url",
                            json={"avatar_url": mxc_url},
                        )
                        self._raise_for_status(avatar_url_resp, "Matrix avatar update failed")
                        self._synced_avatar_signature = avatar_signature
                        changed = True
            if changed:
                logger.info(
                    "matrix adapter: agent profile synced",
                    extra={"extra_data": {"account_id": self.account_id}},
                )
        except Exception:
            logger.warning(
                "matrix adapter: profile sync failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

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

    async def _handle_event(
        self,
        room_id: str,
        event: dict[str, Any],
        *,
        room_data: dict[str, Any] | None = None,
    ) -> None:
        """Process a Matrix room event."""
        if event.get("type") != "m.room.message":
            return

        sender = event.get("sender", "")
        if self._should_ignore_sender(sender):
            return

        content = _event_content(event)
        msgtype = content.get("msgtype", "")
        if msgtype == "m.notice":
            return
        relates_to = _relates_to(content)
        if relates_to.get("rel_type") == "m.replace" or "m.new_content" in content:
            return

        body = content.get("body", "")
        if not isinstance(body, str):
            body = ""
        if not body and not self._has_media(content):
            return
        body = _strip_matrix_reply_fallback(body) if body else ""
        if not body and not self._has_media(content):
            return

        event_id = event.get("event_id", "")
        if not self._remember_event_id(event_id):
            return

        timestamp = event.get("origin_server_ts", 0)
        if self._is_startup_replay(timestamp):
            return

        chat_type = await self._chat_type_for_room(room_id, room_data)
        if chat_type == "group" and not self._room_is_allowed(room_id):
            return

        # Reply context
        reply_to_id = None
        in_reply_to = relates_to.get("m.in_reply_to", {})
        if in_reply_to:
            reply_to_id = in_reply_to.get("event_id")

        # Thread context
        thread_id = None
        if relates_to.get("rel_type") == "m.thread":
            thread_id = relates_to.get("event_id")

        was_mentioned = self._was_mentioned(content, body)
        unmentioned_thread_followup = bool(
            thread_id and chat_type != "direct" and not was_mentioned
        )
        if (
            self._require_mention
            and chat_type != "direct"
            and not was_mentioned
            and not unmentioned_thread_followup
            and not self._group_context_enabled
        ):
            return

        # Parse media
        media: list[MediaAttachment] = []
        if msgtype in {"m.image", "m.video", "m.audio", "m.file"}:
            media_url = content.get("url")
            encrypted_file = content.get("file") if isinstance(content.get("file"), dict) else {}
            if encrypted_file:
                logger.info(
                    "matrix adapter: skipping encrypted media without E2EE support",
                    extra={"extra_data": {"account_id": self.account_id, "room_id": room_id}},
                )
            else:
                media_info = content.get("info") if isinstance(content.get("info"), dict) else {}
                filename = content.get("body")
                media.append(
                    MediaAttachment(
                        url=media_url,
                        platform_id=media_url,
                        mime_type=media_info.get("mimetype"),
                        filename=filename,
                        size_bytes=media_info.get("size"),
                    )
                )
                if body and body == filename:
                    body = ""
        if not body and not media:
            return

        chat_name = await self._chat_name_for_room(
            room_id,
            chat_type=chat_type,
            sender=sender,
            thread_id=thread_id,
            room_data=room_data,
        )
        platform_data: dict[str, Any] = {"event": event}
        if _is_matrix_voice_message(content):
            platform_data["voice_input"] = True
        if thread_id:
            platform_data["thread_root_event_id"] = thread_id
            if unmentioned_thread_followup:
                platform_data["unmentioned_thread_followup_candidate"] = True
            if not self._group_context_enabled:
                root_context = await self._thread_root_context(room_id, thread_id)
                if root_context is not None:
                    platform_data["thread_root"] = root_context
        if timestamp:
            platform_data["_cognis_ordering_key"] = f"{int(timestamp):020d}"
            platform_data["_cognis_ordering_source"] = "provider"

        message = InboundMessage(
            channel_type="matrix",
            account_id=self.account_id,
            message_id=event_id,
            sender_id=sender,
            sender_name=sender.split(":")[0].lstrip("@") if ":" in sender else sender,
            chat_id=room_id,
            chat_type=chat_type,
            chat_name=chat_name,
            content=body,
            reply_to_id=reply_to_id,
            thread_id=thread_id,
            media=media,
            was_mentioned=was_mentioned,
            timestamp=datetime.fromtimestamp(timestamp / 1000, tz=UTC)
            if timestamp
            else datetime.now(UTC),
            platform_data=platform_data,
        )

        await self._dispatch_inbound(message)

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        if self._client is None or not attachment.url or not attachment.url.startswith("mxc://"):
            return None
        _, rest = attachment.url.split("mxc://", 1)
        try:
            server_name, media_id = rest.split("/", 1)
        except ValueError:
            return None
        # Prefer the authenticated media endpoint (MSC3916 / Synapse ≥1.95).
        # Fall back to the legacy unauthenticated path for older homeservers.
        resp = await self._client.get(f"/_matrix/client/v1/media/download/{server_name}/{media_id}")
        if resp.status_code == 404:
            resp = await self._client.get(f"/_matrix/media/v3/download/{server_name}/{media_id}")
        self._raise_for_status(resp, "Matrix attachment download failed")
        return (
            resp.content,
            attachment.mime_type or resp.headers.get("content-type", "application/octet-stream"),
            attachment.filename or media_id,
        )

    def _remember_event_id(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids[event_id] = None
        if len(self._seen_event_ids) > _SEEN_EVENT_LIMIT:
            self._seen_event_ids.popitem(last=False)
        return True

    def _room_is_allowed(self, room_id: str) -> bool:
        return not self._allow_rooms or room_id in self._allow_rooms

    def _is_direct_room(self, room_id: str) -> bool:
        """Match outbound DM routing to the authoritative inbound classification."""
        if room_id in self._group_rooms:
            return False
        return room_id in self._direct_rooms or self._room_type_cache.get(room_id) == "direct"

    def _is_startup_replay(self, timestamp: Any) -> bool:
        if not self._suppress_startup_replay or self._live_sync_established:
            return False
        try:
            event_ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        return bool(event_ts and event_ts < self._started_at_ms)

    def _should_ignore_sender(self, sender: str) -> bool:
        if not sender:
            return True
        if self._user_id and sender.casefold() == self._user_id.casefold():
            return True
        if sender in self._ignored_senders:
            return True
        if self._ignore_appservice_senders and sender.startswith("@_"):
            return True
        return any(pattern.search(sender) for pattern in self._ignored_sender_patterns)

    async def _chat_type_for_room(self, room_id: str, room_data: dict[str, Any] | None) -> str:
        if room_id in self._group_rooms:
            return "group"
        if room_id in self._direct_rooms:
            return "direct"
        if room_id in self._allow_rooms:
            return "group"
        cached = self._room_type_cache.get(room_id)
        if cached:
            return cached
        if self._room_name(room_data):
            self._room_type_cache[room_id] = "group"
            return "group"
        summary = room_data.get("summary", {}) if room_data else {}
        joined_count = summary.get("m.joined_member_count")
        invited_count = summary.get("m.invited_member_count") or 0
        try:
            total_members = int(joined_count) + int(invited_count)
        except (TypeError, ValueError):
            total_members = await self._fetch_joined_member_count(room_id)
            if total_members is None:
                return "group"
        chat_type = "direct" if total_members <= 2 else "group"
        self._room_type_cache[room_id] = chat_type
        return chat_type

    async def _fetch_joined_member_count(self, room_id: str) -> int | None:
        if self._client is None:
            return None
        with contextlib.suppress(Exception):
            resp = await self._client.get(f"/_matrix/client/v3/rooms/{room_id}/joined_members")
            self._raise_for_status(resp, "Matrix joined members lookup failed")
            data = resp.json()
            joined = data.get("joined")
            if isinstance(joined, dict):
                return len(joined)
        return None

    async def _chat_name_for_room(
        self,
        room_id: str,
        *,
        chat_type: str,
        sender: str,
        thread_id: str | None,
        room_data: dict[str, Any] | None,
    ) -> str:
        if chat_type == "direct":
            title = await self._display_name_for_user(sender, room_data)
        else:
            room_name = self._room_name(room_data)
            if room_name:
                title = room_name
            elif thread_id:
                # No room name available (e.g. small private group room classified
                # via allowed_rooms).  Use the sender's display name so the thread
                # conversation title reads "Alice · thread …" instead of the raw
                # room ID.
                title = await self._display_name_for_user(sender, room_data) or room_id
            else:
                title = room_id
        if thread_id:
            title = f"{title} · thread {thread_id[:12]}"
        return title

    async def _display_name_for_user(
        self,
        user_id: str,
        room_data: dict[str, Any] | None,
    ) -> str:
        if not user_id:
            return ""
        cached = self._profile_display_names.get(user_id)
        if cached:
            return cached
        display_name = self._room_member_display_name(user_id, room_data)
        if display_name is None:
            display_name = await self._fetch_user_display_name(user_id)
        if not display_name:
            display_name = user_id.split(":", 1)[0].lstrip("@") if ":" in user_id else user_id
        self._profile_display_names[user_id] = display_name
        if len(self._profile_display_names) > 512:
            self._profile_display_names.popitem(last=False)
        return display_name

    def _room_member_display_name(
        self,
        user_id: str,
        room_data: dict[str, Any] | None,
    ) -> str | None:
        for event in _room_state_events(room_data):
            if event.get("type") != "m.room.member" or event.get("state_key") != user_id:
                continue
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            display_name = content.get("displayname")
            if isinstance(display_name, str) and display_name.strip():
                return display_name.strip()
        return None

    async def _fetch_user_display_name(self, user_id: str) -> str | None:
        if self._client is None:
            return None
        with contextlib.suppress(Exception):
            encoded = quote(user_id, safe="")
            resp = await self._client.get(f"/_matrix/client/v3/profile/{encoded}/displayname")
            self._raise_for_status(resp, "Matrix display name lookup failed")
            data = resp.json()
            display_name = data.get("displayname")
            if isinstance(display_name, str) and display_name.strip():
                return display_name.strip()
        return None

    async def _thread_root_context(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        event = await self._fetch_room_event(room_id, event_id)
        if event is None:
            return {"event_id": event_id}
        content = event.get("content")
        if not isinstance(content, dict):
            content = {}
        body = content.get("body")
        result = {
            "event_id": event_id,
            "sender": event.get("sender"),
            "msgtype": content.get("msgtype"),
            "body": body if isinstance(body, str) else None,
        }
        timestamp = event.get("origin_server_ts")
        if timestamp is not None:
            result["timestamp"] = timestamp
        return result

    async def _fetch_room_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        if self._client is None:
            return None
        with contextlib.suppress(Exception):
            encoded_room = quote(room_id, safe="")
            encoded_event = quote(event_id, safe="")
            resp = await self._client.get(
                f"/_matrix/client/v3/rooms/{encoded_room}/event/{encoded_event}"
            )
            self._raise_for_status(resp, "Matrix room event lookup failed")
            data = resp.json()
            return data if isinstance(data, dict) else None
        return None

    def _room_name(self, room_data: dict[str, Any] | None) -> str | None:
        for event in _room_state_events(room_data):
            event_type = event.get("type")
            content = event.get("content")
            if not isinstance(content, dict):
                continue
            if event_type == "m.room.name":
                name = content.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
            if event_type == "m.room.canonical_alias":
                alias = content.get("alias")
                if isinstance(alias, str) and alias.strip():
                    return alias.strip()
        return None

    def _was_mentioned(self, content: dict[str, Any], body: str) -> bool:
        mentions = content.get("m.mentions")
        if isinstance(mentions, dict):
            user_ids = mentions.get("user_ids")
            if isinstance(user_ids, list) and self._user_id in user_ids:
                return True
        formatted = content.get("formatted_body")
        haystacks = [body]
        if isinstance(formatted, str):
            haystacks.append(_strip_html_tags(formatted))
        if self._user_id and any(
            self._user_id.casefold() in haystack.casefold() for haystack in haystacks
        ):
            return True
        if self._display_name and any(
            _contains_phrase_mention(haystack, self._display_name) for haystack in haystacks
        ):
            return True
        localpart = self._user_id.split(":", 1)[0].lstrip("@") if self._user_id else ""
        return bool(
            localpart
            and any(_contains_localpart_mention(haystack, localpart) for haystack in haystacks)
        )

    def _has_media(self, content: dict[str, Any]) -> bool:
        return content.get("msgtype") in {"m.image", "m.video", "m.audio", "m.file"}

    def _outbound_relates_to(
        self,
        thread_id: str | None,
        reply_to_id: str | None,
    ) -> dict[str, Any] | None:
        if thread_id:
            relates: dict[str, Any] = {
                "rel_type": "m.thread",
                "event_id": thread_id,
                "is_falling_back": bool(reply_to_id),
            }
            if reply_to_id:
                relates["m.in_reply_to"] = {"event_id": reply_to_id}
            return relates
        if reply_to_id:
            return {"m.in_reply_to": {"event_id": reply_to_id}}
        return None

    def _extract_outbound_mentions(self, message: OutboundMessage) -> set[str]:
        raw_user_ids = message.platform_data.get("matrix_mentions")
        if isinstance(raw_user_ids, list):
            return {str(user_id) for user_id in raw_user_ids if str(user_id).startswith("@")}
        return set(re.findall(r"@[A-Za-z0-9._=\-/]+:[A-Za-z0-9.-]+", message.content))

    async def _join_room(self, room_id: str) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            resp = await self._client.post(f"/_matrix/client/v3/rooms/{room_id}/join", json={})
            self._raise_for_status(resp, "Matrix room join failed")

    async def _fetch_display_name(self) -> str:
        if self._client is None or not self._user_id:
            return ""
        with contextlib.suppress(Exception):
            resp = await self._client.get(f"/_matrix/client/v3/profile/{self._user_id}/displayname")
            if resp.status_code == 200:
                data = resp.json()
                display_name = data.get("displayname")
                if isinstance(display_name, str):
                    return display_name
        return ""

    async def _fetch_direct_rooms(self) -> set[str]:
        if self._client is None or not self._user_id:
            return set()
        with contextlib.suppress(Exception):
            resp = await self._client.get(
                f"/_matrix/client/v3/user/{self._user_id}/account_data/m.direct"
            )
            if resp.status_code != 200:
                return set()
            data = resp.json()
            direct_rooms: set[str] = set()
            for room_ids in data.values():
                if isinstance(room_ids, list):
                    direct_rooms.update(str(room_id) for room_id in room_ids if room_id)
            return direct_rooms
        return set()


def _room_state_events(room_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not room_data:
        return []
    events: list[dict[str, Any]] = []
    for section in ("state", "timeline"):
        value = room_data.get(section)
        if not isinstance(value, dict):
            continue
        raw_events = value.get("events")
        if not isinstance(raw_events, list):
            continue
        events.extend(event for event in raw_events if isinstance(event, dict))
    return events
