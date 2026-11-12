"""Explicit process-lifetime composition; inference only sees the memory sink."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.usage.capture_models import UsageDrainResult
from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.price_publisher import (
    UsagePricePinCache,
    UsagePricePublisher,
    UsagePricePublisherDiagnostics,
)
from cognis.core.usage.projector import UsageProjectionDiagnostics, UsageProjector
from cognis.core.usage.quota import UsageQuotaDiagnostics, UsageQuotaRecorder
from cognis.core.usage.service import UsageAccountingService
from cognis.core.usage.settings import load_usage_retention_policy
from cognis.models.config import ModelInfo
from cognis.models.usage_pricing import UsagePricingLookup
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.store.usage.transactions import notify_usage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageRuntimeDiagnostics:
    """Local health supplements, and never replaces, durable coverage epochs."""

    writer: UsageDrainResult
    projection: UsageProjectionDiagnostics
    quota: UsageQuotaDiagnostics
    pricing: UsagePricePublisherDiagnostics | None
    capture_rejected: int
    capture_failed: int
    capture_reduced: int
    shutdown_failed: bool


class UsageRuntime:
    """One canonical writer and one low-priority projector per controller."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, controller_owner_id: str
    ) -> None:
        self.controller_owner_id = controller_owner_id
        self._sessions = sessions
        self.price_cache = UsagePricePinCache()
        self.price_publisher: UsagePricePublisher | None = None
        self._capture: UsageCapture | None = None
        self.projector = UsageProjector(sessions, controller_owner_id=controller_owner_id)
        self.service = UsageAccountingService(
            sessions,
            controller_owner_id=controller_owner_id,
            projection_waker=self.projector.wake,
            capture_gap=self._capture_gapped,
        )
        self.maintenance = UsageMaintenanceService(
            sessions,
            projector=self.projector,
            controller_owner_id=controller_owner_id,
            replay_window_days=self.service.replay_window_days,
            retention_loader=load_usage_retention_policy,
        )
        self.projector.attach_maintenance(self.maintenance)
        self.quota = UsageQuotaRecorder(self.service.try_emit_provider_sample)
        self._closed = False
        self._started = False
        self._shutdown_failed = False
        self._close_task: asyncio.Task[None] | None = None

    def _capture_gapped(self) -> bool:
        capture = self._capture
        return bool(
            (
                capture
                and (capture.rejected_events or capture.failed_events or capture.reduced_contexts)
            )
            or self.quota.invalid_windows
            or self.quota.limited_reports
            or self.quota.rejected_samples
            or self.quota.failed_samples
        )

    @property
    def price_lookup(self) -> UsagePricingLookup:
        """Expose the single committed memory snapshot as Capture's public dependency."""
        return self.price_cache

    @property
    def capture(self) -> UsageCapture:
        """Expose the one public capture object only after the writer has started."""
        if self._capture is None:
            raise RuntimeError("Usage runtime has not started")
        return self._capture

    @property
    def diagnostics(self) -> UsageRuntimeDiagnostics:
        """All omitted/rejected telemetry and stopped collectors remain visible."""
        return UsageRuntimeDiagnostics(
            self.service.diagnostics(),
            self.projector.diagnostics,
            self.quota.diagnostics,
            self.price_publisher.diagnostics if self.price_publisher is not None else None,
            self._capture.rejected_events if self._capture else 0,
            self._capture.failed_events if self._capture else 0,
            self._capture.reduced_contexts if self._capture else 0,
            self._shutdown_failed,
        )

    def wake(self) -> None:
        """Commit/reconnect/settings signals only; no polling or capacity wait."""
        self.projector.wake()

    async def notify_settings(self, session: AsyncSession) -> None:
        """Publish an identity-free hint atomically with the typed setting update."""
        await notify_usage(session, self.controller_owner_id, f"usettings_{uuid.uuid4().hex}")

    async def start(self) -> None:
        """Start supervised asynchronous collectors after schema bootstrap."""
        if self._closed:
            raise RuntimeError("Usage runtime is closed")
        if self._started:
            return
        self._started = True
        await self.service.start()
        self._capture = UsageCapture(
            sink=self.service,
            controller_owner_id=self.service.controller_owner_id,
            coverage_epoch_id=self.service.coverage_epoch_id,
            price_lookup=self.price_lookup,
        )
        await self.projector.start()

    async def start_pricing(self, *, resolve_model: Callable[..., Awaitable[ModelInfo]]) -> None:
        """Attach normalized metadata after constructing the provider facade."""
        if self._closed or not self._started or self.price_publisher is not None:
            raise RuntimeError("Usage pricing must start once after the runtime")
        self.price_publisher = UsagePricePublisher(
            self._sessions,
            maintenance=self.maintenance,
            cache=self.price_cache,
            resolve_model=resolve_model,
        )
        await self.price_publisher.start()

    async def aclose(self) -> None:
        """Stop after producers; durable pending projections resume on next startup."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(), name="usage-runtime-close")
        elif self._close_task.done() and self.service.diagnostics().shutdown_pending:
            self._close_task = asyncio.create_task(
                self.service.aclose(), name="usage-runtime-writer-rejoin"
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if not self._started:
            return
        # Keep the projection notification target usable through canonical drain.
        try:
            if self.price_publisher is not None:
                try:
                    await self.price_publisher.aclose()
                    self._shutdown_failed |= self.price_publisher.diagnostics.running
                except Exception as exc:
                    self._shutdown_failed = True
                    logger.warning("Usage price publisher shutdown failed: %s", type(exc).__name__)
            try:
                await self.service.aclose()
                self._shutdown_failed |= self.service.diagnostics().shutdown_pending
            except Exception as exc:
                self._shutdown_failed = True
                logger.warning("Usage writer shutdown failed: %s", type(exc).__name__)
        finally:
            try:
                await self.projector.aclose()
                self._shutdown_failed |= self.projector.diagnostics.running
            except Exception as exc:
                self._shutdown_failed = True
                logger.warning("Usage projector shutdown failed: %s", type(exc).__name__)
