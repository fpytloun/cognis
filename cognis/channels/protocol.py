"""Channel adapter protocol and base implementation.

Every channel adapter implements the ``ChannelAdapter`` protocol.  The
``BaseChannelAdapter`` provides shared logic for reconnection, status
tracking, and rate limiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from prometheus_client import Counter, Gauge, Histogram

from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelAccountConfig,
    ChannelAccountStatus,
    ChannelCapabilities,
    ChannelRecipient,
    ChannelStatus,
    InboundMessage,
    MediaAttachment,
    OutboundMessage,
    ResolvedChannelTarget,
)
from cognis.models.config import ProviderHealth

logger = get_logger(__name__)


class NonRetryableChannelError(Exception):
    """Fatal channel error that should not enter the reconnect loop."""


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

CHANNEL_INBOUND_TOTAL = Counter(
    "cognis_channel_inbound_total",
    "Inbound messages received from channels",
    ["channel_type", "account_id"],
)
CHANNEL_OUTBOUND_TOTAL = Counter(
    "cognis_channel_outbound_total",
    "Outbound messages sent to channels",
    ["channel_type", "account_id"],
)
CHANNEL_DELIVERY_ERRORS = Counter(
    "cognis_channel_delivery_errors_total",
    "Failed outbound message deliveries",
    ["channel_type", "account_id"],
)
CHANNEL_CONNECTIONS_ACTIVE = Gauge(
    "cognis_channel_connections_active",
    "Currently connected channel accounts",
    ["channel_type"],
)
CHANNEL_RECONNECTIONS = Counter(
    "cognis_channel_reconnections_total",
    "Channel reconnection attempts",
    ["channel_type", "account_id"],
)
CHANNEL_DELIVERY_DURATION = Histogram(
    "cognis_channel_delivery_duration_seconds",
    "Time to deliver an outbound message",
    ["channel_type"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

# Type alias for the inbound message callback
InboundCallback = Callable[[InboundMessage], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ChannelAdapter(Protocol):
    """Protocol for channel adapters.

    Each adapter manages a single account on a single platform.
    The adapter is responsible for:
    - Connecting to the platform (long-poll, WebSocket, SSE, etc.)
    - Receiving inbound messages and normalizing to ``InboundMessage``
    - Sending outbound messages via ``send_message()``
    - Providing typing indicators and read receipts where supported
    - Verifying webhook signatures for platforms that push
    """

    channel_type: str
    capabilities: ChannelCapabilities

    async def start(
        self,
        config: ChannelAccountConfig,
        credentials: dict[str, str],
        on_message: InboundCallback,
    ) -> None:
        """Start the adapter and begin receiving messages.

        Args:
            config: Account configuration from DB.
            credentials: Resolved credential values (decrypted).
            on_message: Callback for inbound messages.
        """
        ...

    async def stop(self) -> None:
        """Stop the adapter and disconnect cleanly."""
        ...

    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message to the platform.

        Returns the platform message ID if available.
        """
        ...

    async def resolve_recipient(
        self,
        recipient: ChannelRecipient,
        *,
        resolution_key: str,
    ) -> ResolvedChannelTarget:
        """Resolve an explicit recipient without exposing provider identifiers."""
        ...

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator to a chat."""
        ...

    async def mark_read(self, chat_id: str, message_id: str) -> None:
        """Mark a message as read on the platform."""
        ...

    async def get_status(self) -> ChannelAccountStatus:
        """Return current runtime status."""
        ...

    async def health(self) -> ProviderHealth:
        """Return health status for monitoring."""
        ...

    async def sync_profile(self, profile: AgentProfile) -> None:
        """Sync agent identity (name, avatar) to the platform."""
        ...

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        """Download inbound media content for normalization into Cognis artifacts."""
        ...

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Verify a webhook signature from the platform.

        Only relevant for webhook-based channels (Telegram, Slack, etc.).
        Returns True if the signature is valid.
        """
        ...


# ---------------------------------------------------------------------------
# Base implementation
# ---------------------------------------------------------------------------

# Reconnection constants
_INITIAL_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 300.0  # 5 minutes
_BACKOFF_MULTIPLIER = 2.0
_MAX_RECONNECT_ATTEMPTS = 50


class BaseChannelAdapter(ABC):
    """Base class for channel adapters with shared lifecycle logic.

    Provides:
    - Status tracking
    - Reconnection with exponential backoff
    - Inbound rate limiting (per-sender)
    - Metrics instrumentation
    """

    channel_type: str
    capabilities: ChannelCapabilities

    def __init__(self) -> None:
        self._config: ChannelAccountConfig | None = None
        self._credentials: dict[str, str] = {}
        self._on_message: InboundCallback | None = None
        self._status = ChannelStatus.DISCONNECTED
        self._connected_at: datetime | None = None
        self._last_message_at: datetime | None = None
        self._last_error: str | None = None
        self._reconnect_attempts = 0
        self._stop_event = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None

        # Per-sender rate limiting (sender_id → last_message_time)
        self._sender_timestamps: dict[str, list[float]] = {}
        self._max_messages_per_minute = 30
        self._inbound_observation_epoch = time.time_ns()
        self._inbound_observation_counter = 0

    @property
    def account_id(self) -> str:
        return self._config.account_id if self._config else ""

    async def start(
        self,
        config: ChannelAccountConfig,
        credentials: dict[str, str],
        on_message: InboundCallback,
    ) -> None:
        """Start the adapter with reconnection support."""
        self._config = config
        self._credentials = credentials
        self._on_message = on_message
        self._stop_event.clear()
        self._reconnect_attempts = 0
        self._set_status(ChannelStatus.CONNECTING)

        self._connection_task = asyncio.create_task(
            self._connection_loop(),
            name=f"channel-{self.channel_type}-{config.account_id}",
        )

    async def stop(self) -> None:
        """Stop the adapter and cancel the connection loop."""
        self._stop_event.set()
        if self._connection_task is not None:
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
            self._connection_task = None
        await self._disconnect()
        self._set_status(ChannelStatus.STOPPED)
        CHANNEL_CONNECTIONS_ACTIVE.labels(channel_type=self.channel_type).dec()

    async def get_status(self) -> ChannelAccountStatus:
        return ChannelAccountStatus(
            account_id=self.account_id,
            channel_type=self.channel_type,
            status=self._status,
            enabled=self._config.enabled if self._config else False,
            connected_at=self._connected_at,
            last_message_at=self._last_message_at,
            last_error=self._last_error,
            reconnect_attempts=self._reconnect_attempts,
        )

    async def health(self) -> ProviderHealth:
        if self._status == ChannelStatus.CONNECTED:
            return ProviderHealth(status="healthy")
        if self._status in {ChannelStatus.CONNECTING, ChannelStatus.RECONNECTING}:
            return ProviderHealth(status="degraded", detail=self._last_error)
        return ProviderHealth(status="unhealthy", detail=self._last_error)

    async def send_typing(self, chat_id: str) -> None:  # noqa: B027
        """Default no-op. Override in adapters that support typing."""

    async def resolve_recipient(
        self,
        recipient: ChannelRecipient,
        *,
        resolution_key: str,
    ) -> ResolvedChannelTarget:
        """Adapters opt into directory resolution explicitly."""
        del recipient, resolution_key
        raise NonRetryableChannelError("Recipient resolution is unsupported")

    async def mark_read(self, chat_id: str, message_id: str) -> None:  # noqa: B027
        """Default no-op. Override in adapters that support read receipts."""

    async def sync_profile(self, profile: AgentProfile) -> None:  # noqa: B027
        """Default no-op. Override in adapters that support profile sync."""

    async def verify_webhook(
        self,
        headers: dict[str, str],
        body: bytes,
        secret: str,
    ) -> bool:
        """Default: reject all webhooks. Override for webhook-based channels."""
        return False

    async def download_attachment(
        self,
        message: InboundMessage,
        attachment: MediaAttachment,
    ) -> tuple[bytes, str, str] | None:
        """Default no-op. Override in adapters that can fetch media."""
        return None

    # ------------------------------------------------------------------
    # Abstract methods for concrete adapters
    # ------------------------------------------------------------------

    @abstractmethod
    async def _connect(self) -> None:
        """Establish connection to the platform.

        Called by the connection loop. Should raise on failure.
        """
        ...

    @abstractmethod
    async def _disconnect(self) -> None:
        """Disconnect from the platform.

        Called on stop and before reconnection.
        """
        ...

    @abstractmethod
    async def _run(self) -> None:
        """Main event loop for receiving messages.

        Called after successful ``_connect()``. Should run until
        the connection drops or ``_stop_event`` is set. On connection
        loss, raise an exception to trigger reconnection.
        """
        ...

    @abstractmethod
    async def send_message(self, message: OutboundMessage) -> str | None:
        """Send a message. Must be implemented by each adapter."""
        ...

    # ------------------------------------------------------------------
    # Connection loop with exponential backoff
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """Connection loop with automatic reconnection."""
        while not self._stop_event.is_set():
            try:
                await self._connect()
                self._set_status(ChannelStatus.CONNECTED)
                self._connected_at = datetime.now(UTC)
                self._reconnect_attempts = 0
                CHANNEL_CONNECTIONS_ACTIVE.labels(channel_type=self.channel_type).inc()

                logger.info(
                    "channel adapter connected",
                    extra={
                        "extra_data": {
                            "channel_type": self.channel_type,
                            "account_id": self.account_id,
                        }
                    },
                )

                await self._run()

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                self._last_error = str(exc)[:200]
                if self._status == ChannelStatus.CONNECTED:
                    CHANNEL_CONNECTIONS_ACTIVE.labels(channel_type=self.channel_type).dec()

                if self._stop_event.is_set():
                    break

                if isinstance(exc, NonRetryableChannelError):
                    self._set_status(ChannelStatus.ERROR)
                    logger.error(
                        "channel adapter fatal error: %s",
                        self._last_error,
                        extra={
                            "extra_data": {
                                "channel_type": self.channel_type,
                                "account_id": self.account_id,
                                "last_error": self._last_error,
                            }
                        },
                    )
                    break

                self._reconnect_attempts += 1
                CHANNEL_RECONNECTIONS.labels(
                    channel_type=self.channel_type,
                    account_id=self.account_id,
                ).inc()

                if self._reconnect_attempts > _MAX_RECONNECT_ATTEMPTS:
                    self._set_status(ChannelStatus.ERROR)
                    logger.error(
                        "channel adapter max reconnect attempts exceeded: %s",
                        self._last_error,
                        extra={
                            "extra_data": {
                                "channel_type": self.channel_type,
                                "account_id": self.account_id,
                                "attempts": self._reconnect_attempts,
                                "last_error": self._last_error,
                            }
                        },
                    )
                    break

                backoff = min(
                    _INITIAL_BACKOFF_SECONDS
                    * (_BACKOFF_MULTIPLIER ** (self._reconnect_attempts - 1)),
                    _MAX_BACKOFF_SECONDS,
                )
                self._set_status(ChannelStatus.RECONNECTING)
                logger.warning(
                    "channel adapter reconnecting: %s (attempt %d, backoff %.1fs)",
                    self._last_error,
                    self._reconnect_attempts,
                    backoff,
                    extra={
                        "extra_data": {
                            "channel_type": self.channel_type,
                            "account_id": self.account_id,
                            "attempt": self._reconnect_attempts,
                            "backoff_seconds": backoff,
                            "last_error": self._last_error,
                        }
                    },
                )

                with contextlib.suppress(Exception):
                    await self._disconnect()

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=backoff,
                    )
                    break  # Stop event was set during backoff
                except TimeoutError:
                    pass  # Backoff elapsed, retry

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, status: ChannelStatus) -> None:
        self._status = status

    async def _dispatch_inbound(self, message: InboundMessage) -> None:
        """Dispatch an inbound message with rate limiting."""
        if self._on_message is None:
            return
        if "_cognis_ordering_key" not in message.platform_data:
            self._inbound_observation_counter += 1
            message.platform_data["_cognis_ordering_key"] = (
                f"{self._inbound_observation_epoch:020d}:{self._inbound_observation_counter:020d}"
            )
            message.platform_data["_cognis_ordering_source"] = "observed"

        # Per-sender rate limiting
        now = asyncio.get_running_loop().time()
        timestamps = self._sender_timestamps.setdefault(message.sender_id, [])
        # Remove timestamps older than 60 seconds
        cutoff = now - 60.0
        timestamps[:] = [t for t in timestamps if t > cutoff]

        # Clean up empty entries to prevent unbounded growth
        if not timestamps:
            del self._sender_timestamps[message.sender_id]
            timestamps = self._sender_timestamps.setdefault(message.sender_id, [])

        if len(timestamps) >= self._max_messages_per_minute:
            logger.warning(
                "channel inbound rate limited",
                extra={
                    "extra_data": {
                        "channel_type": self.channel_type,
                        "account_id": self.account_id,
                        "sender_id": message.sender_id,
                    }
                },
            )
            return

        timestamps.append(now)
        self._last_message_at = datetime.now(UTC)
        CHANNEL_INBOUND_TOTAL.labels(
            channel_type=self.channel_type,
            account_id=self.account_id,
        ).inc()

        await self._on_message(message)
