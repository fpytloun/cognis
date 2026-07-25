"""Remote channel adapter proxy — routes channel operations to an executor.

When a channel account has ``adapter_location="executor"``, the controller
does not run the adapter locally.  Instead it creates a
``RemoteChannelAdapterProxy`` that translates every adapter method into a
JSON-RPC call over the executor WebSocket.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from cognis.channels.protocol import (
    CHANNEL_DELIVERY_ERRORS,
    CHANNEL_OUTBOUND_TOTAL,
)
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelAccountStatus,
    ChannelCapabilities,
    ChannelStatus,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
)
from cognis.models.config import ProviderHealth
from cognis.providers.executor.websocket import ExecutorDisconnectedError

logger = get_logger(__name__)


class RemoteChannelAdapterProxy:
    """Proxy that routes channel operations to an executor via JSON-RPC.

    Satisfies the same interface as ``BaseChannelAdapter`` so the
    ``ChannelManager`` can treat local and remote adapters uniformly.
    """

    def __init__(
        self,
        *,
        connection: Any,  # WebSocketExecutorConnection
        channel_type: str,
        capabilities: ChannelCapabilities,
        account_id: str,
        reconnect_connection: Callable[[], Awaitable[Any | None]] | None = None,
    ) -> None:
        self._connection = connection
        self.channel_type = channel_type
        self.capabilities = capabilities
        self._account_id = account_id
        self._reconnect_connection = reconnect_connection
        self._status = ChannelStatus.DISCONNECTED
        self._connected_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def account_id(self) -> str:
        return self._account_id

    def _connection_for_retry(self) -> Any:
        """Return the live connection to a superseded proxy retry waiter."""

        return self._connection

    async def start(
        self,
        config: ChannelAccountConfig,
        credentials: dict[str, str],
        on_message: Any,
    ) -> None:
        """Send channel.start to the executor."""
        self._status = ChannelStatus.CONNECTING
        try:
            await self._connection.rpc_call(
                "channel.start",
                {
                    "account_id": config.account_id,
                    "channel_type": config.channel_type,
                    "config": {
                        "display_name": config.display_name,
                        "agent_id": config.agent_id,
                        "default_agent_profile_id": config.default_agent_profile_id,
                        "user_email": config.user_email,
                        "settings": config.settings,
                        "default_conversation_id": config.default_conversation_id,
                        "allow_new_conversations": config.allow_new_conversations,
                        "allowed_senders": config.allowed_senders,
                        "dm_policy": config.dm_policy,
                        "group_policy": config.group_policy,
                        "webhook_secret": config.webhook_secret,
                    },
                    "credentials": credentials,
                },
                timeout=30.0,
            )
            self._status = ChannelStatus.CONNECTED
            self._connected_at = datetime.now(UTC)
            logger.info(
                "remote channel proxy: started on executor",
                extra={
                    "extra_data": {
                        "account_id": config.account_id,
                        "channel_type": config.channel_type,
                        "executor_id": self._connection.executor_id,
                    }
                },
            )
        except Exception as exc:
            self._status = ChannelStatus.ERROR
            self._last_error = str(exc)[:200]
            raise

    async def stop(self) -> None:
        """Send channel.stop to the executor."""
        try:
            await self._connection.rpc_call(
                "channel.stop",
                {"account_id": self._account_id},
                timeout=10.0,
            )
        except Exception:
            logger.warning(
                "remote channel proxy: stop failed",
                extra={"extra_data": {"account_id": self._account_id}},
                exc_info=True,
            )
        self._status = ChannelStatus.STOPPED

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send channel.send to the executor."""
        try:
            result = await self._connection.rpc_call(
                "channel.send",
                {
                    "account_id": self._account_id,
                    "message": message.model_dump(mode="json"),
                },
                timeout=30.0,
            )
            CHANNEL_OUTBOUND_TOTAL.labels(
                channel_type=self.channel_type,
                account_id=self._account_id,
            ).inc()
            return result.get("platform_message_id")
        except Exception:
            CHANNEL_DELIVERY_ERRORS.labels(
                channel_type=self.channel_type,
                account_id=self._account_id,
            ).inc()
            logger.warning(
                "remote channel proxy: send failed",
                extra={"extra_data": {"account_id": self._account_id}},
                exc_info=True,
            )
            return None

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator to the executor."""
        try:
            await self._connection.rpc_call(
                "channel.typing",
                {"account_id": self._account_id, "chat_id": chat_id},
                timeout=5.0,
            )
        except Exception:
            logger.debug(
                "remote channel proxy: typing failed",
                extra={"extra_data": {"account_id": self._account_id}},
                exc_info=True,
            )

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Send read receipt to the executor."""
        try:
            await self._connection.rpc_call(
                "channel.mark_read",
                {
                    "account_id": self._account_id,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
                timeout=5.0,
            )
        except Exception:
            logger.debug(
                "remote channel proxy: mark_read failed",
                extra={"extra_data": {"account_id": self._account_id}},
                exc_info=True,
            )

    async def sync_profile(self, profile: Any) -> None:
        """Sync agent profile to the executor."""
        from cognis.models.channel import AgentProfile

        if not isinstance(profile, AgentProfile):
            return
        try:
            payload: dict[str, Any] = {
                "account_id": self._account_id,
                "name": profile.effective_name,
            }
            if profile.avatar_bytes:
                import base64 as b64mod

                payload["avatar_b64"] = b64mod.b64encode(profile.avatar_bytes).decode("ascii")
                payload["avatar_content_type"] = profile.avatar_content_type or "image/png"
            await self._connection.rpc_call(
                "channel.sync_profile",
                payload,
                timeout=15.0,
            )
        except Exception:
            logger.debug(
                "remote channel proxy: sync_profile failed",
                extra={"extra_data": {"account_id": self._account_id}},
                exc_info=True,
            )

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        return await self._download_attachment(
            message,
            attachment,
            stt_supported_mime_types=None,
            raise_errors=False,
        )

    async def download_attachment_for_stt(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
        *,
        supported_mime_types: list[str] | None = None,
    ) -> tuple[bytes, str, str] | None:
        return await self._download_attachment(
            message,
            attachment,
            stt_supported_mime_types=supported_mime_types,
            raise_errors=True,
        )

    async def _download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
        *,
        stt_supported_mime_types: list[str] | None,
        raise_errors: bool,
    ) -> tuple[bytes, str, str] | None:
        params = {
            "account_id": self._account_id,
            "message": message.model_dump(mode="json"),
            "attachment": attachment.model_dump(mode="json"),
            "stt_supported_mime_types": stt_supported_mime_types,
        }

        async def fetch() -> dict[str, Any]:
            result = await self._connection.rpc_call(
                "channel.fetch_media",
                params,
                timeout=60.0,
            )
            if not isinstance(result, dict):
                raise RuntimeError("Executor returned an invalid channel.fetch_media response")
            return result

        try:
            try:
                result = await fetch()
            except ExecutorDisconnectedError:
                if self._reconnect_connection is None:
                    raise
                replacement = await self._reconnect_connection()
                if replacement is None:
                    raise
                self._connection = replacement
                logger.info(
                    "remote channel proxy: retrying fetch_media after executor reconnect",
                    extra={
                        "extra_data": {
                            "account_id": self._account_id,
                            "executor_id": replacement.executor_id,
                        }
                    },
                )
                result = await fetch()
            error = result.get("error")
            if isinstance(error, str) and error:
                raise RuntimeError(error)
            payload = result.get("content_b64")
            if not isinstance(payload, str):
                return None
            return (
                base64.b64decode(payload),
                str(
                    result.get("content_type") or attachment.mime_type or "application/octet-stream"
                ),
                str(result.get("filename") or attachment.filename or "attachment"),
            )
        except Exception:
            logger.warning(
                "remote channel proxy: fetch_media failed",
                extra={"extra_data": {"account_id": self._account_id}},
                exc_info=True,
            )
            if raise_errors:
                raise
            return None

    async def get_status(self) -> ChannelAccountStatus:
        return ChannelAccountStatus(
            account_id=self._account_id,
            channel_type=self.channel_type,
            status=self._status,
            enabled=True,
            connected_at=self._connected_at,
            last_error=self._last_error,
        )

    async def health(self) -> ProviderHealth:
        if self._status == ChannelStatus.CONNECTED:
            return ProviderHealth(status="healthy")
        if self._status in {ChannelStatus.CONNECTING, ChannelStatus.RECONNECTING}:
            return ProviderHealth(status="degraded", detail=self._last_error)
        return ProviderHealth(status="unhealthy", detail=self._last_error)

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Webhook verification is always handled on the controller side."""
        return False

    def update_status(self, status_data: dict[str, Any]) -> None:
        """Update cached status from a channel.status notification."""
        raw = status_data.get("status", "")
        if raw in {s.value for s in ChannelStatus}:
            self._status = ChannelStatus(raw)
        if status_data.get("last_error"):
            self._last_error = str(status_data["last_error"])[:200]
