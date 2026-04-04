"""Composite executor provider — routes to the correct sub-provider.

Dispatches ``spawn``, ``get_executor``, ``cancel``, etc. based on the
``executor_type`` field.  This replaces the hardcoded
``InProcessExecutorProvider`` in the provider registry.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.executor_policy import (
    ExecutorPolicy,
    ensure_executor_type_allowed,
    load_executor_policy,
)
from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.tool import ExecutorConfig, ExecutorHandle
from cognis.providers.executor.in_process import InProcessExecutorProvider
from cognis.providers.executor.subprocess import SubprocessExecutorProvider
from cognis.providers.executor.websocket import WebSocketExecutorProvider

_logger = get_logger(__name__)


class CompositeExecutorProvider:
    """Routes executor operations to the correct sub-provider by type.

    Supported executor types:
    - ``in_process`` — runs tools in the controller process (MVP default)
    - ``subprocess`` — spawns a local Python process
    - ``websocket`` — connects to a remote executor via WebSocket
    """

    def __init__(
        self,
        in_process: InProcessExecutorProvider,
        websocket: WebSocketExecutorProvider,
        subprocess: SubprocessExecutorProvider,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._in_process = in_process
        self._websocket = websocket
        self._subprocess = subprocess
        self._session_factory = session_factory
        # Track executor_id → type for routing get_executor/cancel
        self._handle_types: dict[str, str] = {}

    @property
    def in_process(self) -> InProcessExecutorProvider:
        """Direct access to the in-process sub-provider."""
        return self._in_process

    @property
    def websocket(self) -> WebSocketExecutorProvider:
        """Direct access to the WebSocket sub-provider."""
        return self._websocket

    # ------------------------------------------------------------------
    # ExecutorProvider protocol
    # ------------------------------------------------------------------

    async def spawn(self, config: ExecutorConfig) -> ExecutorHandle:
        """Spawn an executor using the appropriate sub-provider."""
        executor_type = config.metadata.get("executor_type", "in_process")
        if self._session_factory is not None:
            policy = await load_executor_policy(self._session_factory)
            ensure_executor_type_allowed(executor_type, policy)
        provider = self._get_provider(executor_type)
        handle = await provider.spawn(config)
        self._handle_types[handle.executor_id] = executor_type
        return handle

    async def get_executor(self, handle: ExecutorHandle) -> Any:
        """Get the live connection for an executor handle."""
        executor_type = self._handle_types.get(handle.executor_id, handle.executor_type)
        if self._session_factory is not None:
            policy = await load_executor_policy(self._session_factory)
            ensure_executor_type_allowed(executor_type, policy)
        provider = self._get_provider(executor_type)
        return await provider.get_executor(handle)

    async def cancel(self, handle: ExecutorHandle) -> None:
        """Cancel an executor."""
        executor_type = self._handle_types.pop(handle.executor_id, handle.executor_type)
        provider = self._get_provider(executor_type)
        await provider.cancel(handle)

    async def list_active(self) -> list[ExecutorHandle]:
        """List all active executors across all sub-providers."""
        results: list[ExecutorHandle] = []
        results.extend(await self._in_process.list_active())
        results.extend(await self._websocket.list_active())
        results.extend(await self._subprocess.list_active())
        return results

    async def cleanup(self) -> None:
        """Clean up all sub-providers."""
        await self._in_process.cleanup()
        await self._subprocess.cleanup()
        await self._websocket.cleanup()
        self._handle_types.clear()

    async def apply_policy(self, policy: ExecutorPolicy) -> None:
        """Enforce deployment policy on already-active local executors."""
        if not policy.allow_in_process:
            await self._in_process.cleanup()
        if not policy.allow_subprocess:
            await self._subprocess.cleanup()

    async def health(self) -> ProviderHealth:
        """Aggregate health from all sub-providers."""
        ip_health = await self._in_process.health()
        ws_health = await self._websocket.health()
        sp_health = await self._subprocess.health()

        # Overall status: healthy if any sub-provider is healthy
        statuses = [ip_health.status, ws_health.status, sp_health.status]
        if "healthy" in statuses:
            overall = "healthy"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "unhealthy"

        return ProviderHealth(
            name="executor",
            status=overall,
            details={
                "in_process": ip_health.details,
                "websocket": ws_health.details,
                "subprocess": sp_health.details,
            },
        )

    # ------------------------------------------------------------------
    # Extra methods (not on protocol)
    # ------------------------------------------------------------------

    def get_lsp_managers(self) -> list[Any]:
        """Delegate to in-process provider (only it has LSP managers)."""
        return self._in_process.get_lsp_managers()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_provider(self, executor_type: str) -> Any:
        """Resolve the sub-provider for an executor type."""
        if executor_type == "in_process":
            return self._in_process
        if executor_type == "subprocess":
            return self._subprocess
        if executor_type == "websocket":
            return self._websocket
        msg = f"Unknown executor type: {executor_type}"
        raise ValueError(msg)
