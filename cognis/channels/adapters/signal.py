"""Signal adapter — supports REST API and direct signal-cli JSON-RPC transports.

Two transport modes:
- ``rest_api``: Uses an external signal-cli REST API (default, backward-compatible).
- ``direct_jsonrpc``: Runs signal-cli directly on the executor via stdio JSON-RPC.
  Requires ``adapter_location="executor"`` and executor config with
  ``signal.direct_enabled=true``.  The signal-cli command comes from
  executor config (``signal.command``), never from per-account metadata.

Required credentials:
- account_number: E.164 phone number linked to signal-cli (both modes)
- api_url: URL of the signal-cli REST API (rest_api mode only)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import mimetypes
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cognis.channels.adapters.signal_cli_runtime import (
    _ATTACHMENT_TIMEOUT_S,
    _MAX_ATTACHMENT_BYTES,
    SignalCliRuntime,
    SignalCliRuntimeError,
)
from cognis.channels.protocol import BaseChannelAdapter, NonRetryableChannelError
from cognis.channels.registry import SIGNAL_META
from cognis.channels.signal_formatting import format_for_signal, to_signal_text_styles
from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_SIGNAL_DEBUG_ENABLED = _env_flag("COGNIS_SIGNAL_DEBUG", False)
_SIGNAL_MEDIA_PLACEHOLDER = "\u200b"


def _is_fatal_signal_error(message: str) -> bool:
    lowered = message.lower()
    fatal_markers = (
        "is not registered",
        "unregistered user",
        "unknown account",
        "account is not registered",
    )
    return any(marker in lowered for marker in fatal_markers)


# ---------------------------------------------------------------------------
# Internal typed config
# ---------------------------------------------------------------------------


class _SignalConfig:
    """Parsed Signal adapter configuration with safe defaults."""

    def __init__(self, settings: dict[str, Any], credentials: dict[str, str]) -> None:
        self.transport: str = str(settings.get("transport", "rest_api"))
        self.account_number: str = credentials.get("account_number", "")
        self.api_url: str = credentials.get("api_url", "").rstrip("/")
        self.trust_mode: str = str(settings.get("trust_mode", "trust-all-known"))
        self.send_read_receipts: bool = _bool(settings.get("send_read_receipts", True))
        self.enable_typing: bool = _bool(settings.get("enable_typing", True))
        self.sync_profile: bool = _bool(settings.get("sync_profile", True))
        self.ignore_stories: bool = _bool(settings.get("ignore_stories", True))
        # Executor-provided signal-cli command (direct mode only)
        self.signal_cli_command: str = str(settings.get("_signal_cli_command", "signal-cli"))
        self.signal_cli_trust_mode: str = _normalize_signal_cli_trust_mode(self.trust_mode)

    @property
    def is_direct(self) -> bool:
        return self.transport == "direct_jsonrpc"


def _bool(value: Any) -> bool:
    """Coerce a value to bool, handling string representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _normalize_signal_cli_trust_mode(value: str) -> str:
    """Map Cognis/UI trust mode values to signal-cli CLI values.

    The existing channel metadata historically exposed values aligned with the
    REST wrapper (`trust-all-known`, `always-trust`, `on-first-use`).
    Direct `signal-cli` expects CLI values (`always`, `on-first-use`, `never`).
    """
    mapping = {
        "trust-all-known": "on-first-use",
        "always-trust": "always",
        "on-first-use": "on-first-use",
        "always": "always",
        "never": "never",
    }
    return mapping.get(value, "on-first-use")


def _infer_signal_voice_input(body: str | None, attachments: list[dict[str, Any]]) -> bool:
    if not attachments:
        return False
    if any(
        any(bool(attachment.get(key)) for key in ("voiceNote", "voiceMessage", "ptt"))
        for attachment in attachments
    ):
        return True
    normalized_body = body or ""
    if normalized_body.strip():
        return False
    audio_attachments = [
        attachment
        for attachment in attachments
        if str(attachment.get("contentType") or "").startswith("audio/")
    ]
    return len(attachments) == 1 and len(audio_attachments) == 1


def _attachment_result_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    attachment_value = result.get("attachment")
    return {
        "result_keys": sorted(str(key) for key in result),
        "has_file": bool(result.get("file")),
        "has_filename": bool(result.get("filename")),
        "has_path": bool(result.get("path")),
        "has_fileName": bool(result.get("fileName")),
        "has_data": isinstance(result.get("data"), str),
        "has_base64": isinstance(result.get("base64"), str),
        "has_content": isinstance(result.get("content"), str),
        "attachment_type": type(attachment_value).__name__
        if attachment_value is not None
        else None,
        "attachment_keys": sorted(str(key) for key in attachment_value)
        if isinstance(attachment_value, dict)
        else [],
    }


def _fallback_attachment_filename(attachment: MediaAttachment) -> str:
    if attachment.filename:
        return attachment.filename
    mime_type = str(attachment.mime_type or "").lower()
    extension_map = {
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
        "audio/aac": ".aac",
        "audio/flac": ".flac",
    }
    extension = extension_map.get(mime_type)
    if extension:
        return f"attachment{extension}"
    return "attachment.bin"


def _extract_direct_attachment_result(
    result: Any,
    attachment: MediaAttachment,
) -> tuple[bytes, str, str] | None:
    if not isinstance(result, dict):
        return None

    def _path_from(mapping: dict[str, Any]) -> str | None:
        for key in ("file", "filename", "path", "fileName", "storedFile", "storedFilename"):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _payload_from(mapping: dict[str, Any]) -> str | None:
        for key in ("base64", "data", "content"):
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    result_path = _path_from(result)
    if result_path:
        path = Path(result_path)
        if path.exists():
            return (
                path.read_bytes(),
                attachment.mime_type or "application/octet-stream",
                attachment.filename or path.name,
            )

    nested = result.get("attachment")
    nested_path = _path_from(nested) if isinstance(nested, dict) else None
    if nested_path:
        path = Path(nested_path)
        if path.exists():
            return (
                path.read_bytes(),
                attachment.mime_type or "application/octet-stream",
                attachment.filename or path.name,
            )

    payload = _payload_from(result)
    if payload is None and isinstance(nested, dict):
        payload = _payload_from(nested)
    if payload is None:
        return None

    try:
        content = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None
    return (
        content,
        attachment.mime_type or "application/octet-stream",
        _fallback_attachment_filename(attachment),
    )


async def _write_signal_temp_attachment(
    temp_dir: str,
    content: bytes,
    media: MediaAttachment,
) -> Path:
    import uuid

    filename = _fallback_attachment_filename(media)
    suffix = Path(filename).suffix or mimetypes.guess_extension(media.mime_type or "") or ".bin"
    path = Path(temp_dir) / f"out-{uuid.uuid4().hex}{suffix}"
    await asyncio.to_thread(path.write_bytes, content)
    return path


# ---------------------------------------------------------------------------
# Signal adapter
# ---------------------------------------------------------------------------


class SignalAdapter(BaseChannelAdapter):
    """Signal Messenger adapter supporting REST API and direct JSON-RPC."""

    channel_type = "signal"
    capabilities: ChannelCapabilities = SIGNAL_META.capabilities

    def __init__(self) -> None:
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._runtime: SignalCliRuntime | None = None
        self._signal_config: _SignalConfig | None = None
        self._account_number: str = ""
        self._api_url: str = ""
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._degraded_capabilities: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle (overrides BaseChannelAdapter hooks)
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Initialize transport based on configured mode."""
        settings = self._config.settings if self._config else {}
        self._signal_config = _SignalConfig(settings, self._credentials)
        self._account_number = self._signal_config.account_number

        if not self._account_number:
            msg = "Signal adapter requires account_number credential"
            raise ValueError(msg)

        if self._signal_config.is_direct:
            await self._connect_direct()
        else:
            await self._connect_rest()

    async def _connect_rest(self) -> None:
        """Initialize REST API transport (existing behavior)."""
        self._api_url = self._signal_config.api_url if self._signal_config else ""
        if not self._api_url:
            msg = "Signal REST API transport requires api_url credential"
            raise ValueError(msg)

        self._client = httpx.AsyncClient(
            base_url=self._api_url,
            timeout=httpx.Timeout(30.0, read=None),
        )
        resp = await self._client.get("/v1/about")
        resp.raise_for_status()

    async def _connect_direct(self) -> None:
        """Initialize direct signal-cli JSON-RPC transport."""
        assert self._signal_config is not None

        # Clean up any leftover temp dir from a previous connection attempt
        if self._temp_dir is not None:
            with contextlib.suppress(Exception):
                self._temp_dir.cleanup()
        self._temp_dir = tempfile.TemporaryDirectory(prefix="cognis-signal-")

        # Reset degraded capabilities on each fresh connection
        self._degraded_capabilities.clear()

        self._runtime = SignalCliRuntime(
            account_number=self._account_number,
            command=self._signal_config.signal_cli_command,
            trust_mode=self._signal_config.signal_cli_trust_mode,
            on_notification=self._handle_direct_notification,
        )
        try:
            await self._runtime.start()
        except Exception as exc:
            # Clean up temp dir on startup failure
            with contextlib.suppress(Exception):
                self._temp_dir.cleanup()
            self._temp_dir = None
            self._runtime = None
            message = str(exc)
            if _is_fatal_signal_error(message):
                raise NonRetryableChannelError(message) from exc
            raise

    async def _disconnect(self) -> None:
        """Disconnect the active transport."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None

        if self._temp_dir is not None:
            with contextlib.suppress(Exception):
                self._temp_dir.cleanup()
            self._temp_dir = None

    async def _run(self) -> None:
        """Run the inbound message loop for the active transport."""
        if self._signal_config and self._signal_config.is_direct:
            await self._run_direct()
        else:
            await self._run_rest()

    # ------------------------------------------------------------------
    # REST transport — inbound
    # ------------------------------------------------------------------

    async def _run_rest(self) -> None:
        """Listen for inbound messages via SSE stream (REST mode)."""
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
                    await self._handle_rest_event(event_data)
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

    # ------------------------------------------------------------------
    # Direct transport — inbound
    # ------------------------------------------------------------------

    async def _run_direct(self) -> None:
        """Keep the direct runtime alive until stop is requested.

        Inbound messages arrive via the runtime's notification callback
        (``_handle_direct_notification``), so this method just waits.
        """
        if self._runtime is None:
            return

        # Wait until stop is requested or the runtime dies
        while not self._stop_event.is_set() and self._runtime.is_running:
            await asyncio.sleep(1.0)

        if not self._stop_event.is_set() and not self._runtime.is_running:
            message = self._runtime._process_exit_message()
            if _is_fatal_signal_error(message):
                raise NonRetryableChannelError(message)
            raise SignalCliRuntimeError(message)

    async def _handle_direct_notification(self, params: dict[str, Any]) -> None:
        """Handle a ``receive`` notification from signal-cli JSON-RPC."""
        envelope = params.get("envelope", {})
        if not envelope:
            return

        # Ignore story messages if configured
        if (
            self._signal_config
            and self._signal_config.ignore_stories
            and envelope.get("storyMessage")
        ):
            return

        data_message = envelope.get("dataMessage")
        if data_message is None:
            return

        await self._process_envelope(envelope, data_message)

    # ------------------------------------------------------------------
    # Outbound — send message
    # ------------------------------------------------------------------

    def _direct_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Normalize params for the current direct signal-cli runtime mode.

        The current runtime starts signal-cli in single-account mode using
        ``signal-cli -a ACCOUNT jsonRpc``. In that mode upstream JSON-RPC does
        not require an ``account`` param, and some commands appear to behave
        poorly when it is sent explicitly.
        """
        if self._runtime is not None and self._runtime.single_account_mode:
            params.pop("account", None)
        return params

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via the active transport."""
        chunks = format_for_signal(message.content, self.capabilities.max_message_length)
        if not chunks:
            chunks = []

        if not chunks and not message.media:
            return None

        last_message_id: str | None = None
        for index, chunk in enumerate(chunks or [None]):
            chunk_message = message.model_copy(
                update={
                    "content": chunk.plain_text if chunk is not None else message.content,
                    "platform_data": {
                        **message.platform_data,
                        "signal_markdown_text": chunk.markdown_text
                        if chunk is not None
                        else message.content,
                        "signal_text_styles": to_signal_text_styles(chunk.plain_text, chunk.styles)
                        if chunk is not None
                        else [],
                    },
                    "media": message.media if index == 0 else [],
                }
            )
            if self._signal_config and self._signal_config.is_direct:
                last_message_id = await self._send_direct(chunk_message)
            else:
                last_message_id = await self._send_rest(chunk_message)
        return last_message_id

    async def _send_rest(self, message: OutboundMessage) -> str | None:
        """Send via REST API."""
        if self._client is None:
            return None

        text = message.platform_data.get("signal_markdown_text", message.content)
        if not text and message.media:
            text = _SIGNAL_MEDIA_PLACEHOLDER

        payload: dict[str, Any] = {
            "message": text,
            "number": self._account_number,
            "recipients": [message.chat_id],
        }

        if payload["message"]:
            payload["text_mode"] = "styled"

        if message.reply_to_id:
            payload["quote_timestamp"] = message.reply_to_id

        if message.media:
            b64_attachments: list[str] = []
            for media in message.media:
                if media.content_b64:
                    b64_attachments.append(media.content_b64)
                    continue
                if not media.url:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=60.0) as dl:
                        resp = await dl.get(media.url)
                        resp.raise_for_status()
                    b64_attachments.append(base64.b64encode(resp.content).decode("ascii"))
                except Exception:
                    logger.warning("signal adapter: media download failed", exc_info=True)
            if b64_attachments:
                payload["base64_attachments"] = b64_attachments
            logger.info(
                "signal adapter: prepared rest media payload",
                extra={
                    "extra_data": {
                        "account_id": self.account_id,
                        "media_count": len(message.media),
                        "encoded_attachment_count": len(b64_attachments),
                    }
                },
            )

        resp = await self._client.post("/v2/send", json=payload)
        resp.raise_for_status()
        result = resp.json()
        return result.get("timestamp")

    async def _send_direct(self, message: OutboundMessage) -> str | None:
        """Send via direct signal-cli JSON-RPC."""
        if self._runtime is None or not self._runtime.is_running:
            return None

        text = message.content or (_SIGNAL_MEDIA_PLACEHOLDER if message.media else "")

        params = self._direct_params(
            {
                "message": text,
                "account": self._account_number,
                "recipient": [message.chat_id],
            }
        )

        text_styles = message.platform_data.get("signal_text_styles")
        if isinstance(text_styles, list) and text_styles:
            params["textStyle"] = text_styles

        if message.reply_to_id:
            logger.debug(
                "signal adapter: direct reply context ignored because signal-cli requires quote author metadata",
                extra={"extra_data": {"account_id": self.account_id}},
            )

        temp_files: list[Path] = []
        if message.media:
            attachments: list[str] = []
            for media in message.media:
                if media.content_b64:
                    try:
                        if self._temp_dir is None:
                            logger.warning(
                                "signal adapter: temp dir unavailable for direct media send",
                                extra={"extra_data": {"account_id": self.account_id}},
                            )
                            continue
                        temp_path = await _write_signal_temp_attachment(
                            self._temp_dir.name,
                            base64.b64decode(media.content_b64),
                            media,
                        )
                        attachments.append(str(temp_path))
                        temp_files.append(temp_path)
                        continue
                    except Exception:
                        logger.warning("signal adapter: inline media decode failed", exc_info=True)
                if not media.url:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=60.0) as dl:
                        resp = await dl.get(media.url)
                        resp.raise_for_status()
                    if self._temp_dir is None:
                        logger.warning(
                            "signal adapter: temp dir unavailable for direct media send",
                            extra={"extra_data": {"account_id": self.account_id}},
                        )
                        continue
                    temp_path = await _write_signal_temp_attachment(
                        self._temp_dir.name,
                        resp.content,
                        media,
                    )
                    attachments.append(str(temp_path))
                    temp_files.append(temp_path)
                except Exception:
                    logger.warning("signal adapter: media download failed", exc_info=True)
            if attachments:
                params["attachment"] = attachments
            logger.info(
                "signal adapter: prepared direct media payload",
                extra={
                    "extra_data": {
                        "account_id": self.account_id,
                        "media_count": len(message.media),
                        "attachment_count": len(attachments),
                        "has_temp_dir": self._temp_dir is not None,
                        "attachment_mode": "temp_file",
                    }
                },
            )

        try:
            result = await self._runtime.request("send", params)
            return str(result.get("timestamp", ""))
        except SignalCliRuntimeError:
            logger.warning(
                "signal adapter: direct send failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )
            return None
        finally:
            for temp_path in temp_files:
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Typing / read receipts / profile sync
    # ------------------------------------------------------------------

    async def sync_profile(self, profile: AgentProfile) -> None:
        """Sync agent profile to Signal."""
        if self._signal_config and not self._signal_config.sync_profile:
            return

        if self._signal_config and self._signal_config.is_direct:
            await self._sync_profile_direct(profile)
        else:
            await self._sync_profile_rest(profile)

    async def _sync_profile_rest(self, profile: AgentProfile) -> None:
        if self._client is None:
            return
        try:
            payload: dict[str, Any] = {"name": profile.effective_name}
            if profile.avatar_bytes:
                payload["avatar"] = base64.b64encode(profile.avatar_bytes).decode("ascii")
            await self._client.put(
                f"/v1/profiles/{self._account_number}",
                json=payload,
            )
            logger.info(
                "signal adapter: agent profile synced",
                extra={"extra_data": {"account_id": self.account_id}},
            )
        except Exception:
            logger.warning(
                "signal adapter: profile sync failed",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

    async def _sync_profile_direct(self, profile: AgentProfile) -> None:
        if self._runtime is None or not self._runtime.is_running:
            return
        if "updateProfile" in self._degraded_capabilities:
            return
        if self._runtime.version is None:
            self._degraded_capabilities.add("updateProfile")
            logger.info(
                "signal adapter: skipping direct profile sync because runtime version could not be probed",
                extra={"extra_data": {"account_id": self.account_id}},
            )
            return
        avatar_path: Path | None = None
        try:
            params = self._direct_params(
                {
                    "account": self._account_number,
                    "givenName": profile.effective_name,
                }
            )
            if profile.avatar_bytes and self._temp_dir:
                import uuid

                avatar_path = Path(self._temp_dir.name) / f"avatar-{uuid.uuid4().hex}"
                await asyncio.to_thread(avatar_path.write_bytes, profile.avatar_bytes)
                params["avatar"] = str(avatar_path)
            await self._runtime.request("updateProfile", params)
            logger.info(
                "signal adapter: agent profile synced (direct)",
                extra={"extra_data": {"account_id": self.account_id}},
            )
        except SignalCliRuntimeError:
            self._degraded_capabilities.add("updateProfile")
            logger.warning(
                "signal adapter: profile sync failed (direct), disabling",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )
        finally:
            if avatar_path is not None:
                with contextlib.suppress(OSError):
                    avatar_path.unlink(missing_ok=True)

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator."""
        if self._signal_config and not self._signal_config.enable_typing:
            return

        if self._signal_config and self._signal_config.is_direct:
            await self._send_typing_direct(chat_id)
        else:
            await self._send_typing_rest(chat_id)

    async def _send_typing_rest(self, chat_id: str) -> None:
        if self._client is None:
            return
        payload = {
            "recipient": chat_id,
            "number": self._account_number,
        }
        with contextlib.suppress(Exception):
            await self._client.put("/v1/typing-indicator/" + self._account_number, json=payload)

    async def _send_typing_direct(self, chat_id: str) -> None:
        if self._runtime is None or not self._runtime.is_running:
            return
        if "sendTyping" in self._degraded_capabilities:
            return
        try:
            await self._runtime.request(
                "sendTyping",
                self._direct_params({"account": self._account_number, "recipient": [chat_id]}),
            )
        except SignalCliRuntimeError:
            self._degraded_capabilities.add("sendTyping")
            logger.debug(
                "signal adapter: typing indicator failed (direct), disabling",
                extra={"extra_data": {"account_id": self.account_id}},
            )

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Mark a message as read."""
        if self._signal_config and not self._signal_config.send_read_receipts:
            return

        if self._signal_config and self._signal_config.is_direct:
            await self._mark_read_direct(chat_id, message_id)
        else:
            await self._mark_read_rest(chat_id, message_id)

    async def _mark_read_rest(self, chat_id: str, message_id: str) -> None:
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

    async def _mark_read_direct(self, chat_id: str, message_id: str) -> None:
        if self._runtime is None or not self._runtime.is_running:
            return
        if "sendReceipt" in self._degraded_capabilities:
            return
        try:
            await self._runtime.request(
                "sendReceipt",
                self._direct_params(
                    {
                        "account": self._account_number,
                        "recipient": chat_id,
                        "type": "read",
                        "targetTimestamp": [int(message_id)],
                    }
                ),
            )
        except (SignalCliRuntimeError, ValueError):
            self._degraded_capabilities.add("sendReceipt")
            logger.debug(
                "signal adapter: read receipt failed (direct), disabling",
                extra={"extra_data": {"account_id": self.account_id}},
            )

    # ------------------------------------------------------------------
    # Event handling (shared normalization)
    # ------------------------------------------------------------------

    async def _handle_rest_event(self, event: dict[str, Any]) -> None:
        """Process a signal-cli REST API event."""
        envelope = event.get("envelope", {})
        if not envelope:
            return

        data_message = envelope.get("dataMessage")
        if data_message is None:
            return

        await self._process_envelope(envelope, data_message)

    async def _process_envelope(
        self, envelope: dict[str, Any], data_message: dict[str, Any]
    ) -> None:
        """Normalize a Signal envelope into an InboundMessage."""
        source = envelope.get("source", "") or envelope.get("sourceNumber", "")
        source_name = envelope.get("sourceName", "")
        timestamp = data_message.get("timestamp", 0)
        body = data_message.get("message") or ""

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

        raw_attachments = list(data_message.get("attachments", []))
        voice_input = _infer_signal_voice_input(body, raw_attachments)
        if _SIGNAL_DEBUG_ENABLED:
            logger.info(
                "signal adapter: inbound attachment metadata",
                extra={
                    "extra_data": {
                        "account_id": self.account_id,
                        "body_empty": not bool(body.strip()),
                        "attachment_count": len(raw_attachments),
                        "attachment_content_types": [
                            str(attachment.get("contentType") or "")
                            for attachment in raw_attachments
                        ],
                        "attachment_has_voice_flags": [
                            {
                                "voiceNote": bool(attachment.get("voiceNote")),
                                "voiceMessage": bool(attachment.get("voiceMessage")),
                                "ptt": bool(attachment.get("ptt")),
                            }
                            for attachment in raw_attachments
                        ],
                        "attachment_has_platform_ids": [
                            bool(attachment.get("id")) for attachment in raw_attachments
                        ],
                        "inferred_voice_input": voice_input,
                    }
                },
            )

        # Parse attachments
        media: list[MediaAttachment] = []
        for attachment in raw_attachments:
            media.append(
                MediaAttachment(
                    path=attachment.get("filename"),
                    platform_id=attachment.get("id"),
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
        if voice_input:
            message.platform_data["voice_input"] = True

        await self._dispatch_inbound(message)

    # ------------------------------------------------------------------
    # Attachment download
    # ------------------------------------------------------------------

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        """Download an inbound attachment."""
        if self._signal_config and self._signal_config.is_direct:
            return await self._download_attachment_direct(message, attachment)
        return await self._download_attachment_rest(message, attachment)

    async def _download_attachment_rest(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        """Download attachment from local filesystem (REST mode)."""
        if not attachment.path:
            return None
        path = Path(attachment.path)
        if not path.exists():
            return None
        content = await asyncio.to_thread(path.read_bytes)
        if len(content) > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "signal adapter: attachment too large",
                extra={
                    "extra_data": {
                        "account_id": self.account_id,
                        "size": len(content),
                        "max": _MAX_ATTACHMENT_BYTES,
                    }
                },
            )
            return None
        return (
            content,
            attachment.mime_type or "application/octet-stream",
            attachment.filename or path.name,
        )

    async def _download_attachment_direct(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        """Download attachment via signal-cli getAttachment (direct mode)."""
        if self._runtime is None or not self._runtime.is_running:
            if _SIGNAL_DEBUG_ENABLED:
                logger.info(
                    "signal adapter: direct attachment download skipped",
                    extra={
                        "extra_data": {
                            "account_id": self.account_id,
                            "reason": "runtime_unavailable",
                        }
                    },
                )
            return None

        attachment_id = attachment.platform_id
        if not attachment_id:
            if _SIGNAL_DEBUG_ENABLED:
                logger.info(
                    "signal adapter: direct attachment missing platform id",
                    extra={
                        "extra_data": {
                            "account_id": self.account_id,
                            "has_path": bool(attachment.path),
                            "mime_type": attachment.mime_type,
                        }
                    },
                )
            # Try filesystem path as fallback
            if attachment.path:
                path = Path(attachment.path)
                if path.exists():
                    content = await asyncio.to_thread(path.read_bytes)
                    if len(content) <= _MAX_ATTACHMENT_BYTES:
                        return (
                            content,
                            attachment.mime_type or "application/octet-stream",
                            attachment.filename or path.name,
                        )
            return None

        try:
            if _SIGNAL_DEBUG_ENABLED:
                logger.info(
                    "signal adapter: direct attachment download starting",
                    extra={
                        "extra_data": {
                            "account_id": self.account_id,
                            "attachment_id_present": True,
                            "mime_type": attachment.mime_type,
                        }
                    },
                )
            result = await self._runtime.request(
                "getAttachment",
                self._direct_params({"account": self._account_number, "id": attachment_id}),
                timeout=_ATTACHMENT_TIMEOUT_S,
            )
            if _SIGNAL_DEBUG_ENABLED:
                logger.info(
                    "signal adapter: direct attachment download result",
                    extra={
                        "extra_data": {
                            "account_id": self.account_id,
                            **_attachment_result_metadata(result),
                        }
                    },
                )
            extracted = await asyncio.to_thread(
                _extract_direct_attachment_result, result, attachment
            )
            if extracted is not None:
                content, mime_type, filename = extracted
                if len(content) > _MAX_ATTACHMENT_BYTES:
                    logger.warning(
                        "signal adapter: attachment too large",
                        extra={
                            "extra_data": {
                                "account_id": self.account_id,
                                "size": len(content),
                            }
                        },
                    )
                    return None
                if _SIGNAL_DEBUG_ENABLED:
                    logger.info(
                        "signal adapter: direct attachment extracted",
                        extra={
                            "extra_data": {
                                "account_id": self.account_id,
                                "filename": filename,
                                "mime_type": mime_type,
                            }
                        },
                    )
                return content, mime_type, filename
        except SignalCliRuntimeError:
            logger.warning(
                "signal adapter: getAttachment failed (direct)",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

        return None
