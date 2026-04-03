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

from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.channels.inbound import InboundPipeline
from cognis.channels.protocol import BaseChannelAdapter, InboundCallback
from cognis.channels.registry import get_channel_meta
from cognis.logging import get_logger
from cognis.models.channel import (
    ChannelAccountConfig,
    ChannelAccountStatus,
    ChannelStatus,
    InboundMessage,
)
from cognis.models.config import ProviderHealth

logger = get_logger(__name__)


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
        ws_provider: Any | None = None,  # WebSocketExecutorProvider (for executor-hosted channels)
    ) -> None:
        self._session_factory = session_factory
        self._inbound_pipeline = inbound_pipeline
        self._secrets_provider = secrets_provider
        self._artifact_store = artifact_store
        self._ws_provider = ws_provider

        # account_id → adapter instance (local BaseChannelAdapter or RemoteChannelAdapterProxy)
        self._adapters: dict[str, Any] = {}
        # account_id → config
        self._configs: dict[str, ChannelAccountConfig] = {}

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

        logger.info(
            "channel manager: startup complete",
            extra={"extra_data": {"started": started, "total": len(rows)}},
        )

    async def stop_all(self) -> None:
        """Stop all running adapters."""
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
        if config.account_id in self._adapters:
            await self.stop_account(config.account_id)

        # Resolve credentials from SecretsProvider
        credentials = await self._resolve_credentials(config)

        # Create inbound callback
        on_message: InboundCallback = self._make_inbound_callback(config)

        if config.adapter_location == "executor":
            adapter = await self._start_executor_adapter(config, credentials, on_message)
        else:
            adapter = _create_adapter(config.channel_type)
            await adapter.start(config, credentials, on_message)

        self._adapters[config.account_id] = adapter
        self._configs[config.account_id] = config

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
        adapter = self._adapters.pop(account_id, None)
        config = self._configs.pop(account_id, None)
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
        return self._adapters.get(account_id)

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
            adapter = self._adapters.get(account_id)
            config = self._configs.get(account_id)
            if adapter and config and config.channel_type == channel_type:
                return adapter, config
            return None

        for aid, adapter in self._adapters.items():
            if adapter.channel_type == channel_type:
                config = self._configs.get(aid)
                if config:
                    return adapter, config
        return None

    # ------------------------------------------------------------------
    # Status and health
    # ------------------------------------------------------------------

    async def get_all_statuses(self) -> list[ChannelAccountStatus]:
        """Get status for all managed accounts."""
        statuses: list[ChannelAccountStatus] = []
        for adapter in self._adapters.values():
            statuses.append(await adapter.get_status())
        return statuses

    async def get_account_status(self, account_id: str) -> ChannelAccountStatus | None:
        """Get status for a specific account."""
        adapter = self._adapters.get(account_id)
        if adapter is None:
            return None
        return await adapter.get_status()

    async def health(self) -> ProviderHealth:
        """Aggregate health across all adapters."""
        if not self._adapters:
            return ProviderHealth(status="healthy", detail="No channel accounts configured")

        connected = sum(
            1
            for a in self._adapters.values()
            if (await a.get_status()).status == ChannelStatus.CONNECTED
        )
        total = len(self._adapters)

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
        adapter = self._adapters.get(account_id)
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

    def _make_inbound_callback(self, config: ChannelAccountConfig) -> InboundCallback:
        """Create an inbound callback that routes through the pipeline."""

        async def on_message(message: InboundMessage) -> None:
            await self._inbound_pipeline.process(message, config)

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
            raise ValueError(msg)

        meta = get_channel_meta(config.channel_type)
        capabilities = meta.capabilities if meta else ChannelCapabilities()

        proxy = RemoteChannelAdapterProxy(
            connection=conn,
            channel_type=config.channel_type,
            capabilities=capabilities,
            account_id=config.account_id,
        )

        # Register notification callbacks so inbound messages from the
        # executor flow through the inbound pipeline.
        async def _on_remote_message(message_data: dict[str, Any]) -> None:
            from cognis.models.channel import InboundMessage

            msg = InboundMessage(**message_data)
            await on_message(msg)

        conn.register_channel_callback(
            config.account_id,
            on_message=_on_remote_message,
            on_status=proxy.update_status,
        )

        await proxy.start(config, credentials, on_message)
        return proxy

    def _find_executor_connection(self, config: ChannelAccountConfig) -> Any | None:
        """Find a connected executor for an executor-hosted channel."""
        if self._ws_provider is None:
            return None

        # If a specific executor is requested, use it
        if config.executor_id:
            return self._ws_provider.get_connection(config.executor_id)

        # Otherwise find any connected executor with channel capability
        for handle in self._ws_provider._handles.values():
            if handle.status == "ready" and handle.capabilities.channels:
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
        user_email=row.user_email,
        settings=row.config or {},
        default_conversation_id=row.default_conversation_id,
        allow_new_conversations=row.allow_new_conversations,
        adapter_location=getattr(row, "adapter_location", None) or "controller",
        executor_id=getattr(row, "executor_id", None),
        allowed_senders=row.allowed_senders or [],
        dm_policy=row.dm_policy or "pairing",
        group_policy=row.group_policy or "pairing",
        webhook_secret=row.webhook_secret,
    )
