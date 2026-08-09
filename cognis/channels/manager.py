"""Channel manager — lifecycle orchestration for channel adapters.

The ChannelManager:
1. Loads all enabled channel accounts from DB on startup
2. Creates adapter instances and starts them
3. Handles reconnection (delegated to BaseChannelAdapter)
4. Provides status monitoring and health checks
5. Supports hot-reload (start/stop individual accounts via API)
6. Routes inbound messages through the inbound pipeline
7. Provides adapter lookup for outbound delivery
"""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.channels.inbound import InboundPipeline
from cognis.channels.protocol import BaseChannelAdapter, InboundCallback
from cognis.channels.registry import get_channel_meta
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.channel import (
    AgentProfile,
    ChannelAccountConfig,
    ChannelAccountStatus,
    ChannelStatus,
    InboundMessage,
)
from cognis.models.config import ProviderHealth
from cognis.store.coordination import (
    DatabaseLeaseStore,
    Lease,
    _lease_expiry_expression,
    database_now_expression,
)
from cognis.store.models import (
    ChannelAccountOperationRow,
    ChannelAccountRow,
    CoordinationLeaseRow,
)

logger = get_logger(__name__)
_CHANNEL_LEASE_TTL_SECONDS = 45.0
_CHANNEL_RECONCILE_SECONDS = 10.0
_CHANNEL_OPERATION_STALE_SECONDS = 120.0
_ACTIVE_CHANNEL_OPERATION: ContextVar[tuple[int, str, int, int] | None] = ContextVar(
    "active_channel_operation",
    default=None,
)


class ExecutorChannelDeferred(RuntimeError):
    """Executor-hosted channel cannot start until its executor connects."""


class ChannelOwnershipLost(RuntimeError):
    """Raised when an adapter operation no longer owns its channel account."""


class _OwnedAdapterView:
    """Lease/generation-bound adapter surface returned to external callers."""

    def __init__(
        self,
        manager: ChannelManager,
        account_id: str,
        generation: int,
        adapter: Any,
    ) -> None:
        self._manager = manager
        self._account_id = account_id
        self._generation = generation
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._adapter, name)
        if not callable(attribute) or not asyncio.iscoroutinefunction(attribute):
            return attribute

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            return await self._manager._run_owned_operation(  # noqa: SLF001
                self._account_id,
                self._generation,
                self._adapter,
                attribute,
                *args,
                **kwargs,
            )

        return guarded


def _create_adapter(channel_type: str) -> BaseChannelAdapter:
    """Create an adapter instance for a channel type."""
    from cognis.channels.factory import create_adapter

    return create_adapter(channel_type)


class ChannelManager:
    """Lifecycle orchestration for channel adapters.

    Manages adapter instances, routes inbound messages through the
    inbound pipeline, and provides adapter lookup for outbound delivery.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[Any],
        inbound_pipeline: InboundPipeline,
        secrets_provider: Any,
        artifact_store: Any,
        event_bus: EventBus,
        ws_provider: Any | None = None,  # WebSocketExecutorProvider (for executor-hosted channels)
        controller_owner_id: str = "simple-controller",
    ) -> None:
        self._session_factory = session_factory
        self._inbound_pipeline = inbound_pipeline
        self._secrets_provider = secrets_provider
        self._artifact_store = artifact_store
        self._event_bus = event_bus
        self._ws_provider = ws_provider
        self._controller_owner_id = controller_owner_id
        self._lease_store = DatabaseLeaseStore(session_factory)

        # account_id → adapter instance (local BaseChannelAdapter or RemoteChannelAdapterProxy)
        self._adapters: dict[str, Any] = {}
        self._adapter_views: dict[str, _OwnedAdapterView] = {}
        # account_id → config
        self._configs: dict[str, ChannelAccountConfig] = {}
        # account_id → cached AgentProfile
        self._agent_profiles: dict[str, AgentProfile] = {}
        self._avatar_refresh_task: asyncio.Task[None] | None = None
        self._ownership_task: asyncio.Task[None] | None = None
        self._leases: dict[str, Lease] = {}
        self._lease_deadlines: dict[str, float] = {}
        self._generations: dict[str, int] = {}
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._avatar_refresh_interval = 4 * 3600  # 4 hours

        # Subscribe to agent profile updates
        event_bus.subscribe(EventType.AGENT_PROFILE_UPDATED, self._handle_agent_profile_updated)

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Load all enabled channel accounts from DB and start them."""
        from cognis.store.queries import list_channel_accounts

        async with self._session_factory() as session:
            rows = await list_channel_accounts(session, enabled_only=True)

        started = 0
        for row in rows:
            config = _row_to_config(row)
            try:
                await self.start_account(config)
                started += 1
            except ExecutorChannelDeferred as exc:
                logger.info(
                    "channel manager: deferred executor-hosted account startup",
                    extra={
                        "extra_data": {
                            "account_id": config.account_id,
                            "channel_type": config.channel_type,
                            "executor_id": config.executor_id,
                            "reason": str(exc),
                        }
                    },
                )
            except Exception:
                logger.exception(
                    "channel manager: failed to start account",
                    extra={
                        "extra_data": {
                            "account_id": config.account_id,
                            "channel_type": config.channel_type,
                        }
                    },
                )

        # Start periodic avatar URL refresh
        if self._avatar_refresh_task is not None:
            self._avatar_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._avatar_refresh_task
        self._avatar_refresh_task = asyncio.create_task(
            self._avatar_refresh_loop(), name="channel-avatar-refresh"
        )
        if self._ownership_task is not None:
            self._ownership_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ownership_task
        self._ownership_task = asyncio.create_task(
            self._ownership_loop(), name="channel-account-ownership"
        )

        logger.info(
            "channel manager: startup complete",
            extra={"extra_data": {"started": started, "total": len(rows)}},
        )

    async def stop_all(self) -> None:
        """Stop all running adapters."""
        if self._ownership_task is not None:
            self._ownership_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ownership_task
            self._ownership_task = None
        if self._avatar_refresh_task is not None:
            self._avatar_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._avatar_refresh_task
            self._avatar_refresh_task = None
        for account_id in list(self._adapters.keys()):
            try:
                await self.stop_account(account_id)
            except Exception:
                logger.exception(
                    "channel manager: failed to stop account",
                    extra={"extra_data": {"account_id": account_id}},
                )

    # ------------------------------------------------------------------
    # Individual account management
    # ------------------------------------------------------------------

    async def start_account(self, config: ChannelAccountConfig) -> None:
        """Start a single channel account (local or executor-hosted)."""
        lock = self._account_locks.setdefault(config.account_id, asyncio.Lock())
        async with lock:
            lease = self._leases.get(config.account_id)
            if lease is None:
                lease_started_at = monotonic()
                lease = await self._lease_store.acquire(
                    f"channel-account:{config.account_id}",
                    self._controller_owner_id,
                    ttl_seconds=_CHANNEL_LEASE_TTL_SECONDS,
                )
                if lease is None:
                    return
                self._leases[config.account_id] = lease
                self._lease_deadlines[config.account_id] = (
                    lease_started_at + _CHANNEL_LEASE_TTL_SECONDS
                )
            if config.account_id in self._adapters:
                await self._stop_local_account(config.account_id, release_lease=False)

            try:
                # Resolve credentials from SecretsProvider only after ownership.
                credentials = await self._resolve_credentials(config)

                # Bind inbound consumption to this exact ownership fence.
                generation = self._generations.get(config.account_id, 0) + 1
                self._generations[config.account_id] = generation
                on_message: InboundCallback = self._make_inbound_callback(config, lease, generation)

                if config.adapter_location == "executor":
                    adapter = await self._start_executor_adapter(config, credentials, on_message)
                else:
                    adapter = _create_adapter(config.channel_type)
                    await adapter.start(config, credentials, on_message)

                if not self._owns_lease(config.account_id, lease):
                    await adapter.stop()
                    return
                self._adapters[config.account_id] = adapter
                self._adapter_views[config.account_id] = _OwnedAdapterView(
                    self,
                    config.account_id,
                    generation,
                    adapter,
                )
                self._configs[config.account_id] = config
            except BaseException:
                self._leases.pop(config.account_id, None)
                self._lease_deadlines.pop(config.account_id, None)
                await self._lease_store.release(lease)
                raise

        # Sync agent profile to the platform (best-effort)
        try:
            profile = await self._resolve_agent_profile(config.agent_id)
            owned_adapter = self.get_adapter(config.account_id)
            if (
                profile is not None
                and owned_adapter is not None
                and self._adapters.get(config.account_id) is adapter
                and self._generations.get(config.account_id) == generation
            ):
                self._agent_profiles[config.account_id] = profile
                await owned_adapter.sync_profile(profile)
        except Exception:
            logger.warning(
                "channel manager: agent profile sync failed on start",
                extra={"extra_data": {"account_id": config.account_id}},
                exc_info=True,
            )

        logger.info(
            "channel manager: account started",
            extra={
                "extra_data": {
                    "account_id": config.account_id,
                    "channel_type": config.channel_type,
                    "location": config.adapter_location,
                }
            },
        )

    async def stop_account(self, account_id: str) -> None:
        """Stop a single channel account."""
        lock = self._account_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            await self._stop_local_account(account_id, release_lease=True)

    async def _stop_local_account(
        self,
        account_id: str,
        *,
        release_lease: bool,
    ) -> None:
        # Invalidate callbacks before adapter.stop() can block or emit a
        # delayed message/status callback. Restart retains the lease but not
        # the old adapter generation.
        self._generations[account_id] = self._generations.get(account_id, 0) + 1
        adapter = self._adapters.pop(account_id, None)
        self._adapter_views.pop(account_id, None)
        config = self._configs.pop(account_id, None)
        self._agent_profiles.pop(account_id, None)
        if adapter is not None:
            await adapter.stop()
            # Unregister channel callbacks if this was an executor-hosted adapter
            if config and config.adapter_location == "executor":
                conn = self._find_executor_connection(config)
                if conn is not None:
                    conn.unregister_channel_callback(account_id)
            logger.info(
                "channel manager: account stopped",
                extra={"extra_data": {"account_id": account_id}},
            )
        if release_lease:
            lease = self._leases.pop(account_id, None)
            self._lease_deadlines.pop(account_id, None)
            if lease is not None:
                await self._lease_store.release(lease)

    async def restart_account(self, account_id: str) -> None:
        """Restart a channel account (reload config from DB)."""
        from cognis.store.queries import get_channel_account

        async with self._session_factory() as session:
            row = await get_channel_account(session, account_id)
        if row is None:
            logger.warning(
                "channel manager: account not found for restart",
                extra={"extra_data": {"account_id": account_id}},
            )
            return
        config = _row_to_config(row)
        await self.start_account(config)

    # ------------------------------------------------------------------
    # Adapter lookup
    # ------------------------------------------------------------------

    def get_adapter(self, account_id: str) -> BaseChannelAdapter | None:
        """Get the adapter for an account."""
        if not self._owns_current_account(account_id):
            return None
        view = self._adapter_views.get(account_id)
        return cast(BaseChannelAdapter | None, view)

    def owns_account(self, account_id: str) -> bool:
        """Return whether this controller owns the current account lease."""
        return self._owns_current_account(account_id)

    def get_config(self, account_id: str) -> ChannelAccountConfig | None:
        """Get the config for an account."""
        return self._configs.get(account_id)

    def find_adapter_for_channel(
        self,
        channel_type: str,
        account_id: str | None = None,
    ) -> tuple[BaseChannelAdapter, ChannelAccountConfig] | None:
        """Find an adapter for a channel type, optionally by account_id.

        If account_id is not specified, returns the first connected
        adapter for the channel type.
        """
        if account_id:
            adapter = self.get_adapter(account_id)
            config = self._configs.get(account_id)
            if adapter and config and config.channel_type == channel_type:
                return adapter, config
            return None

        for aid, adapter in self._adapters.items():
            if not self._owns_current_account(aid):
                continue
            if adapter.channel_type == channel_type:
                config = self._configs.get(aid)
                if config:
                    view = self._adapter_views.get(aid)
                    if view is not None:
                        return cast(BaseChannelAdapter, view), config
        return None

    # ------------------------------------------------------------------
    # Status and health
    # ------------------------------------------------------------------

    async def get_all_statuses(self) -> list[ChannelAccountStatus]:
        """Get status for all managed accounts."""
        statuses: list[ChannelAccountStatus] = []
        for account_id in self._adapters:
            adapter = self.get_adapter(account_id)
            if adapter is not None:
                statuses.append(await adapter.get_status())
        return statuses

    async def get_account_status(self, account_id: str) -> ChannelAccountStatus | None:
        """Get status for a specific account."""
        adapter = self.get_adapter(account_id)
        if adapter is None:
            return None
        return await adapter.get_status()

    async def health(self) -> ProviderHealth:
        """Aggregate health across all adapters."""
        adapters = [
            adapter
            for account_id in self._adapters
            if (adapter := self.get_adapter(account_id)) is not None
        ]
        if not adapters:
            return ProviderHealth(status="healthy", detail="No channel accounts configured")

        connected = sum(
            1 for a in adapters if (await a.get_status()).status == ChannelStatus.CONNECTED
        )
        total = len(adapters)

        if connected == total:
            return ProviderHealth(status="healthy")
        if connected > 0:
            return ProviderHealth(
                status="degraded",
                detail=f"{connected}/{total} accounts connected",
            )
        return ProviderHealth(
            status="unhealthy",
            detail="No channel accounts connected",
        )

    # ------------------------------------------------------------------
    # Webhook handling
    # ------------------------------------------------------------------

    async def handle_webhook(
        self,
        channel_type: str,
        account_id: str,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any] | None:
        """Handle an inbound webhook from a platform.

        Verifies the webhook signature and dispatches to the adapter.
        Returns a response dict for the platform, or None.
        """
        adapter = self.get_adapter(account_id)
        config = self._configs.get(account_id)
        if adapter is None or config is None:
            logger.warning(
                "channel manager: webhook for unknown account",
                extra={
                    "extra_data": {
                        "channel_type": channel_type,
                        "account_id": account_id,
                    }
                },
            )
            return None

        # Verify webhook signature
        secret = config.webhook_secret or ""
        if not await adapter.verify_webhook(headers, body, secret):
            logger.warning(
                "channel manager: webhook signature verification failed",
                extra={
                    "extra_data": {
                        "channel_type": channel_type,
                        "account_id": account_id,
                    }
                },
            )
            return None

        # Delegate to adapter-specific webhook handling
        if hasattr(adapter, "handle_webhook_payload"):
            return await adapter.handle_webhook_payload(body)  # type: ignore[attr-defined]
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_inbound_callback(
        self,
        config: ChannelAccountConfig,
        lease: Lease,
        generation: int,
    ) -> InboundCallback:
        """Create an inbound callback that routes through the pipeline."""

        async def on_message(
            message: InboundMessage,
            *,
            executor_connection_owner: Any | None = None,
        ) -> None:
            if self._inbound_pipeline is None:
                return
            try:
                await self._run_owned_operation(
                    config.account_id,
                    generation,
                    self._adapters.get(config.account_id),
                    self._inbound_pipeline.process,
                    message,
                    config,
                    executor_connection_owner=executor_connection_owner,
                )
            except ChannelOwnershipLost:
                return

        return on_message

    async def _start_executor_adapter(
        self,
        config: ChannelAccountConfig,
        credentials: dict[str, str],
        on_message: InboundCallback,
    ) -> Any:
        """Start a channel adapter on a remote executor."""
        from cognis.channels.remote import RemoteChannelAdapterProxy
        from cognis.models.channel import ChannelCapabilities

        conn = self._find_executor_connection(config)
        if conn is None:
            msg = (
                f"No connected executor available for channel account {config.account_id}. "
                f"Requested executor_id={config.executor_id!r}."
            )
            raise ExecutorChannelDeferred(msg)

        meta = get_channel_meta(config.channel_type)
        capabilities = meta.capabilities if meta else ChannelCapabilities()

        proxy: RemoteChannelAdapterProxy | None = None

        async def _reconnect_connection() -> Any | None:
            if self._ws_provider is None:
                return None
            from cognis.providers.executor.websocket import (
                executor_reconnect_retry_budget_seconds,
            )

            budget = executor_reconnect_retry_budget_seconds()
            deadline = monotonic() + budget
            replacement = await self._ws_provider.wait_for_connection(
                conn.executor_id,
                timeout=budget,
            )
            if replacement is None:
                return None

            deadline = max(deadline, monotonic() + min(10.0, budget))
            while monotonic() < deadline:
                current = self._adapters.get(config.account_id)
                if isinstance(current, RemoteChannelAdapterProxy) and current is not proxy:
                    status = await current.get_status()
                    current_connection = current._connection_for_retry()  # noqa: SLF001
                    if (
                        status.status == ChannelStatus.CONNECTED
                        and current_connection.executor_id == conn.executor_id
                    ):
                        return current_connection
                await asyncio.sleep(0.1)
            return None

        proxy = RemoteChannelAdapterProxy(
            connection=conn,
            channel_type=config.channel_type,
            capabilities=capabilities,
            account_id=config.account_id,
            reconnect_connection=_reconnect_connection,
        )

        # Register notification callbacks so inbound messages from the
        # executor flow through the inbound pipeline.
        async def _on_remote_message(
            connection_owner: Any,
            message_data: dict[str, Any],
        ) -> None:
            from cognis.models.channel import InboundMessage

            msg = InboundMessage(**message_data)
            await on_message(
                msg,
                executor_connection_owner=connection_owner,
            )

        async def _on_remote_status(
            _connection_owner: Any,
            status_data: dict[str, Any],
        ) -> None:
            proxy.update_status(status_data)

        conn.register_channel_callback(
            config.account_id,
            on_message=_on_remote_message,
            on_status=_on_remote_status,
        )

        await proxy.start(config, credentials, on_message)
        return proxy

    def _find_executor_connection(self, config: ChannelAccountConfig) -> Any | None:
        """Find a connected executor for an executor-hosted channel."""
        if self._ws_provider is None:
            return None

        # If a specific executor is requested, use it
        if config.executor_id:
            return self._ws_provider.get_local_connection(config.executor_id)

        # Otherwise find any connected executor with channel capability
        for handle in self._ws_provider.iter_local_ready_handles():
            if handle.capabilities.channels:
                return self._ws_provider.get_connection(handle.executor_id)
        return None

    async def start_executor_channels(self, executor_id: str, conn: Any) -> None:
        """Start all channel accounts assigned to a specific executor.

        Called when an executor connects and is ready.  Loads channel
        accounts from the DB that have ``adapter_location="executor"``
        and either target this executor specifically or have no
        preference (``executor_id IS NULL``).
        """
        from cognis.store.queries import list_channel_accounts

        async with self._session_factory() as session:
            rows = await list_channel_accounts(session, enabled_only=True)

        for row in rows:
            config = _row_to_config(row)
            if config.adapter_location != "executor":
                continue
            if config.executor_id and config.executor_id != executor_id:
                continue
            # Force-restart if already running (handles reconnect with stale proxy)
            try:
                await self.start_account(config)
            except Exception:
                logger.exception(
                    "channel manager: failed to start executor channel",
                    extra={
                        "extra_data": {
                            "account_id": config.account_id,
                            "executor_id": executor_id,
                        }
                    },
                )

    # ------------------------------------------------------------------
    # Agent profile resolution and sync
    # ------------------------------------------------------------------

    async def _resolve_agent_profile(self, agent_id: str) -> AgentProfile | None:
        """Load an agent definition and resolve its profile for channel use."""
        from cognis.store.queries import get_agent

        async with self._session_factory() as session:
            row = await get_agent(session, agent_id)
        if row is None:
            return None

        name = row.name
        display_name = getattr(row, "display_name", None)
        avatar_url: str | None = None
        avatar_bytes: bytes | None = None
        avatar_content_type: str | None = None

        avatar_image_id = getattr(row, "avatar_image_id", None)
        if avatar_image_id and self._artifact_store:
            for artifact_name in ("image", "avatar"):
                try:
                    resolved_url = await self._artifact_store.async_get_signed_url(
                        "avatars",
                        avatar_image_id,
                        artifact_name,
                        ttl_seconds=6 * 3600,
                    )
                    content, ct = await self._artifact_store.async_load(
                        "avatars",
                        avatar_image_id,
                        artifact_name,
                    )
                    avatar_url = resolved_url
                    avatar_bytes = content
                    avatar_content_type = ct
                    break
                except Exception:
                    logger.debug(
                        "channel manager: avatar resolution failed",
                        extra={
                            "extra_data": {
                                "agent_id": agent_id,
                                "avatar_image_id": avatar_image_id,
                                "artifact_name": artifact_name,
                            }
                        },
                        exc_info=True,
                    )

        return AgentProfile(
            name=name,
            display_name=display_name,
            avatar_url=avatar_url,
            avatar_bytes=avatar_bytes,
            avatar_content_type=avatar_content_type,
        )

    async def _handle_agent_profile_updated(self, event: Event) -> None:
        """Re-sync profile on all active adapters bound to the updated agent."""
        agent_id = event.data.get("agent_id")
        if not isinstance(agent_id, str):
            return

        profile = await self._resolve_agent_profile(agent_id)
        if profile is None:
            return

        for account_id, config in self._configs.items():
            if config.agent_id != agent_id:
                continue
            adapter = self.get_adapter(account_id)
            if adapter is None:
                continue
            generation = self._generations.get(account_id)
            try:
                if (
                    self.get_adapter(account_id) is not adapter
                    or self._generations.get(account_id) != generation
                ):
                    continue
                self._agent_profiles[account_id] = profile
                await adapter.sync_profile(profile)
                logger.info(
                    "channel manager: agent profile re-synced",
                    extra={
                        "extra_data": {
                            "account_id": account_id,
                            "agent_id": agent_id,
                        }
                    },
                )
            except Exception:
                logger.warning(
                    "channel manager: agent profile re-sync failed",
                    extra={
                        "extra_data": {
                            "account_id": account_id,
                            "agent_id": agent_id,
                        }
                    },
                    exc_info=True,
                )

    def get_agent_profile(self, account_id: str) -> AgentProfile | None:
        """Return the cached agent profile for an account."""
        return self._agent_profiles.get(account_id)

    async def _avatar_refresh_loop(self) -> None:
        """Periodically re-sign avatar URLs so they don't expire."""
        while True:
            await asyncio.sleep(self._avatar_refresh_interval)
            for account_id, profile in list(self._agent_profiles.items()):
                if not profile.avatar_url:
                    continue
                config = self._configs.get(account_id)
                adapter = self.get_adapter(account_id)
                if config is None or adapter is None:
                    continue
                generation = self._generations.get(account_id)
                try:
                    refreshed = await self._resolve_agent_profile(config.agent_id)
                    if refreshed is None:
                        continue
                    if (
                        self.get_adapter(account_id) is not adapter
                        or self._generations.get(account_id) != generation
                    ):
                        continue
                    self._agent_profiles[account_id] = refreshed
                    await adapter.sync_profile(refreshed)
                except Exception:
                    logger.debug(
                        "channel manager: avatar URL refresh failed",
                        extra={"extra_data": {"account_id": account_id}},
                        exc_info=True,
                    )

    async def _ownership_loop(self) -> None:
        """Renew owned accounts and claim enabled accounts after owner expiry."""
        from cognis.store.queries import list_channel_accounts

        while True:
            await asyncio.sleep(_CHANNEL_RECONCILE_SECONDS)
            for account_id, lease in list(self._leases.items()):
                try:
                    lock = self._account_locks.setdefault(account_id, asyncio.Lock())
                    async with lock:
                        if not self._owns_lease(account_id, lease):
                            self._leases.pop(account_id, None)
                            self._lease_deadlines.pop(account_id, None)
                            await self._stop_local_account(account_id, release_lease=False)
                            continue
                        renew_started_at = monotonic()
                        renewed = await self._lease_store.renew(
                            lease, ttl_seconds=_CHANNEL_LEASE_TTL_SECONDS
                        )
                        if renewed is None:
                            self._leases.pop(account_id, None)
                            self._lease_deadlines.pop(account_id, None)
                            await self._stop_local_account(account_id, release_lease=False)
                        elif self._owns_lease(account_id, lease):
                            self._leases[account_id] = renewed
                            self._lease_deadlines[account_id] = (
                                renew_started_at + _CHANNEL_LEASE_TTL_SECONDS
                            )
                        else:
                            await self._lease_store.release(renewed)
                except Exception:
                    logger.exception(
                        "channel manager: account lease renewal failed",
                        extra={"extra_data": {"account_id": account_id}},
                    )
                    self._leases.pop(account_id, None)
                    self._lease_deadlines.pop(account_id, None)
                    with contextlib.suppress(Exception):
                        await self._stop_local_account(account_id, release_lease=False)

            try:
                async with self._session_factory() as session:
                    rows = await list_channel_accounts(session, enabled_only=True)
            except Exception:
                logger.exception("channel manager: account reconciliation query failed")
                continue
            enabled = {row.account_id for row in rows}
            for account_id in list(self._leases):
                if account_id not in enabled:
                    with contextlib.suppress(Exception):
                        await self.stop_account(account_id)
            for row in rows:
                if row.account_id not in self._leases:
                    try:
                        await self.start_account(_row_to_config(row))
                    except ExecutorChannelDeferred:
                        pass
                    except Exception:
                        logger.exception(
                            "channel manager: account reconciliation start failed",
                            extra={"extra_data": {"account_id": row.account_id}},
                        )

    def _owns_lease(self, account_id: str, lease: Lease) -> bool:
        current = self._leases.get(account_id)
        return (
            current is not None
            and current.owner_id == lease.owner_id
            and current.fencing_token == lease.fencing_token
            and monotonic() < self._lease_deadlines.get(account_id, 0)
        )

    def _owns_current_account(self, account_id: str) -> bool:
        lease = self._leases.get(account_id)
        return lease is not None and self._owns_lease(account_id, lease)

    async def _assert_operation_current(
        self,
        account_id: str,
        generation: int,
        adapter: Any,
    ) -> None:
        lease = self._leases.get(account_id)
        if (
            lease is None
            or self._generations.get(account_id) != generation
            or self._adapters.get(account_id) is not adapter
            or not self._owns_lease(account_id, lease)
        ):
            raise ChannelOwnershipLost(account_id)
        from cognis.store.queries import get_channel_account

        async with self._session_factory() as session:
            account = await get_channel_account(session, account_id)
        if account is None or not account.enabled or not await self._lease_store.is_current(lease):
            raise ChannelOwnershipLost(account_id)

    async def _run_owned_operation(
        self,
        account_id: str,
        generation: int,
        adapter: Any,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Serialize external side effects against durable ownership revocation."""
        current_task = asyncio.current_task()
        operation_key = (id(self), account_id, generation, id(current_task))
        if _ACTIVE_CHANNEL_OPERATION.get() == operation_key:
            return await operation(*args, **kwargs)
        lease = self._leases.get(account_id)
        if (
            lease is None
            or self._generations.get(account_id) != generation
            or self._adapters.get(account_id) is not adapter
            or not self._owns_lease(account_id, lease)
        ):
            raise ChannelOwnershipLost(account_id)
        await self._admit_operation(account_id, lease)
        token = _ACTIVE_CHANNEL_OPERATION.set(operation_key)
        try:
            return await operation(*args, **kwargs)
        finally:
            _ACTIVE_CHANNEL_OPERATION.reset(token)
            await self._finish_operation(account_id, lease)

    async def _admit_operation(self, account_id: str, lease: Lease) -> None:
        async with self._session_factory() as session:
            now = database_now_expression(session)
            lease_row = (
                await session.execute(
                    update(CoordinationLeaseRow)
                    .where(
                        CoordinationLeaseRow.resource_key == lease.resource_key,
                        CoordinationLeaseRow.owner_id == lease.owner_id,
                        CoordinationLeaseRow.fencing_token == lease.fencing_token,
                        CoordinationLeaseRow.lease_expires_at > now,
                    )
                    .values(updated_at=CoordinationLeaseRow.updated_at)
                    .returning(CoordinationLeaseRow.resource_key)
                )
            ).scalar_one_or_none()
            account_enabled = await session.scalar(
                select(ChannelAccountRow.enabled).where(ChannelAccountRow.account_id == account_id)
            )
            if lease_row is None or not account_enabled:
                await session.rollback()
                raise ChannelOwnershipLost(account_id)
            statement = select(ChannelAccountOperationRow).where(
                ChannelAccountOperationRow.account_id == account_id
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            state = (await session.execute(statement)).scalar_one_or_none()
            if state is None:
                state = ChannelAccountOperationRow(
                    account_id=account_id,
                    owner_id=lease.owner_id,
                    fencing_token=lease.fencing_token,
                    active_count=1,
                    expires_at=_lease_expiry_expression(session, _CHANNEL_OPERATION_STALE_SECONDS),
                    updated_at=datetime.now(UTC),
                )
                session.add(state)
            else:
                if state.owner_id != lease.owner_id or state.fencing_token != lease.fencing_token:
                    now = database_now_expression(session)
                    stale = bool(
                        await session.scalar(
                            select(ChannelAccountOperationRow.account_id).where(
                                ChannelAccountOperationRow.account_id == account_id,
                                ChannelAccountOperationRow.expires_at <= now,
                            )
                        )
                    )
                    if state.active_count > 0 and not stale:
                        await session.rollback()
                        raise ChannelOwnershipLost(account_id)
                    state.owner_id = lease.owner_id
                    state.fencing_token = lease.fencing_token
                    state.active_count = 0
                state.active_count += 1
                state.expires_at = _lease_expiry_expression(
                    session, _CHANNEL_OPERATION_STALE_SECONDS
                )
                state.updated_at = datetime.now(UTC)
            await session.commit()

    async def _finish_operation(self, account_id: str, lease: Lease) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(ChannelAccountOperationRow)
                .where(
                    ChannelAccountOperationRow.account_id == account_id,
                    ChannelAccountOperationRow.owner_id == lease.owner_id,
                    ChannelAccountOperationRow.fencing_token == lease.fencing_token,
                    ChannelAccountOperationRow.active_count > 0,
                )
                .values(
                    active_count=ChannelAccountOperationRow.active_count - 1,
                    updated_at=database_now_expression(session),
                )
            )
            await session.commit()

    async def revoke_account(self, account_id: str) -> bool:
        """Revoke ownership globally and drain the local adapter if present."""
        owned_locally = account_id in self._adapters
        await self._lease_store.revoke(f"channel-account:{account_id}")
        await self.stop_account(account_id)
        drained = await self.wait_until_relinquished(account_id, timeout_seconds=2.0)
        return owned_locally and drained

    async def wait_until_relinquished(
        self,
        account_id: str,
        *,
        timeout_seconds: float,
    ) -> bool:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            async with self._session_factory() as session:
                now = database_now_expression(session)
                await session.execute(
                    update(ChannelAccountOperationRow)
                    .where(
                        ChannelAccountOperationRow.account_id == account_id,
                        ChannelAccountOperationRow.active_count > 0,
                        ChannelAccountOperationRow.expires_at <= now,
                    )
                    .values(
                        active_count=0,
                        updated_at=now,
                    )
                )
                active_count = await session.scalar(
                    select(ChannelAccountOperationRow.active_count).where(
                        ChannelAccountOperationRow.account_id == account_id
                    )
                )
                await session.commit()
            if not active_count:
                return True
            await asyncio.sleep(0.05)
        return False

    async def stop_executor_channels(self, executor_id: str) -> None:
        """Stop all channel accounts hosted on a disconnected executor.

        Called when an executor WebSocket disconnects to clean up stale
        ``RemoteChannelAdapterProxy`` instances.
        """
        from cognis.channels.remote import RemoteChannelAdapterProxy

        to_stop: list[str] = []
        for account_id, adapter in self._adapters.items():
            if isinstance(adapter, RemoteChannelAdapterProxy):
                config = self._configs.get(account_id)
                if config and (
                    config.executor_id == executor_id
                    or (
                        not config.executor_id
                        and getattr(adapter._connection, "executor_id", None) == executor_id
                    )
                ):
                    to_stop.append(account_id)

        for account_id in to_stop:
            try:
                await self.stop_account(account_id)
                logger.info(
                    "channel manager: stopped executor channel on disconnect",
                    extra={
                        "extra_data": {
                            "account_id": account_id,
                            "executor_id": executor_id,
                        }
                    },
                )
            except Exception:
                logger.exception(
                    "channel manager: failed to stop executor channel",
                    extra={
                        "extra_data": {
                            "account_id": account_id,
                            "executor_id": executor_id,
                        }
                    },
                )

    async def _resolve_credentials(
        self,
        config: ChannelAccountConfig,
    ) -> dict[str, str]:
        """Resolve credential references to actual values."""
        credentials: dict[str, str] = {}
        for ref_name, secret_name in config.credential_refs.items():
            try:
                value = await self._secrets_provider.get_secret(
                    name=secret_name,
                    user_id=config.user_email,
                    agent_id=config.agent_id,
                )
                credentials[ref_name] = value
            except Exception:
                logger.warning(
                    "channel manager: failed to resolve credential",
                    extra={
                        "extra_data": {
                            "account_id": config.account_id,
                            "ref_name": ref_name,
                        }
                    },
                )
        # Also include non-secret settings that adapters need as credentials
        # (e.g., API URLs, phone numbers)
        for key, value in config.settings.items():
            if key not in credentials and isinstance(value, str):
                credentials[key] = value
        return credentials


def _row_to_config(row: Any) -> ChannelAccountConfig:
    """Convert a DB row to a ChannelAccountConfig."""
    return ChannelAccountConfig(
        account_id=row.account_id,
        channel_type=row.channel_type,
        display_name=row.display_name,
        enabled=row.enabled,
        credential_refs=row.credential_refs or {},
        agent_id=row.agent_id,
        default_agent_profile_id=getattr(row, "default_agent_profile_id", None),
        user_email=row.user_email,
        settings=row.config or {},
        default_conversation_id=row.default_conversation_id,
        allow_new_conversations=row.allow_new_conversations,
        preferred_for_task_delivery=getattr(row, "preferred_for_task_delivery", False),
        adapter_location=getattr(row, "adapter_location", None) or "controller",
        executor_id=getattr(row, "executor_id", None),
        allowed_senders=row.allowed_senders or [],
        dm_policy=row.dm_policy or "pairing",
        group_policy=row.group_policy or "pairing",
        webhook_secret=row.webhook_secret,
    )
