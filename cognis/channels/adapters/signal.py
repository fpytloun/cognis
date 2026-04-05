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
import contextlib
import json
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
from cognis.channels.protocol import BaseChannelAdapter
from cognis.channels.registry import SIGNAL_META
from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelCapabilities,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)

logger = get_logger(__name__)


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
            trust_mode=self._signal_config.trust_mode,
            on_notification=self._handle_direct_notification,
        )
        try:
            await self._runtime.start()
        except Exception:
            # Clean up temp dir on startup failure
            with contextlib.suppress(Exception):
                self._temp_dir.cleanup()
            self._temp_dir = None
            self._runtime = None
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
            raise SignalCliRuntimeError("signal-cli process exited unexpectedly")

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

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message via the active transport."""
        if self._signal_config and self._signal_config.is_direct:
            return await self._send_direct(message)
        return await self._send_rest(message)

    async def _send_rest(self, message: OutboundMessage) -> str | None:
        """Send via REST API."""
        if self._client is None:
            return None

        payload: dict[str, Any] = {
            "message": message.content,
            "number": self._account_number,
            "recipients": [message.chat_id],
        }

        if message.reply_to_id:
            payload["quote_timestamp"] = message.reply_to_id

        if message.media:
            b64_attachments: list[str] = []
            for media in message.media:
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

        resp = await self._client.post("/v2/send", json=payload)
        resp.raise_for_status()
        result = resp.json()
        return result.get("timestamp")

    async def _send_direct(self, message: OutboundMessage) -> str | None:
        """Send via direct signal-cli JSON-RPC."""
        if self._runtime is None or not self._runtime.is_running:
            return None

        params: dict[str, Any] = {
            "message": message.content,
            "account": self._account_number,
            "recipient": [message.chat_id],
        }

        if message.reply_to_id:
            with contextlib.suppress(ValueError):
                params["quoteTimestamp"] = int(message.reply_to_id)

        temp_files: list[Path] = []
        if message.media:
            attachments: list[str] = []
            for media in message.media:
                if not media.url:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=60.0) as dl:
                        resp = await dl.get(media.url)
                        resp.raise_for_status()
                    # Write to temp file for signal-cli
                    if self._temp_dir:
                        import uuid

                        fname = Path(self._temp_dir.name) / f"out-{uuid.uuid4().hex}"
                        await asyncio.to_thread(fname.write_bytes, resp.content)
                        attachments.append(str(fname))
                        temp_files.append(fname)
                except Exception:
                    logger.warning("signal adapter: media download failed", exc_info=True)
            if attachments:
                params["attachment"] = attachments

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
            # Clean up temp files immediately after send
            for tf in temp_files:
                with contextlib.suppress(OSError):
                    tf.unlink(missing_ok=True)

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
        avatar_path: Path | None = None
        try:
            params: dict[str, Any] = {
                "account": self._account_number,
                "givenName": profile.effective_name,
            }
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
                {"account": self._account_number, "recipient": [chat_id]},
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
                {
                    "account": self._account_number,
                    "recipient": chat_id,
                    "type": "read",
                    "targetTimestamp": [int(message_id)],
                },
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
            return None

        attachment_id = attachment.platform_id
        if not attachment_id:
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
            result = await self._runtime.request(
                "getAttachment",
                {"account": self._account_number, "id": attachment_id},
                timeout=_ATTACHMENT_TIMEOUT_S,
            )
            # signal-cli may return the attachment path
            att_path = result.get("file") or result.get("filename")
            if att_path:
                path = Path(att_path)
                if path.exists():
                    content = await asyncio.to_thread(path.read_bytes)
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
                    return (
                        content,
                        attachment.mime_type or "application/octet-stream",
                        attachment.filename or path.name,
                    )
        except SignalCliRuntimeError:
            logger.warning(
                "signal adapter: getAttachment failed (direct)",
                extra={"extra_data": {"account_id": self.account_id}},
                exc_info=True,
            )

        return None
