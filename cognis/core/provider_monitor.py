"""Shared, lease-fenced provider observations without inference.

The database owns snapshots and deadlines. A per-provider lease covers both manual
and scheduled I/O; publication checks its fence and configuration in one short
transaction. Lifespan owns cancellation. Failures preserve last good observations.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.models import ProviderMonitorResponse, ProviderUsageResponse
from cognis.core.inbox import stage_notification
from cognis.core.maintenance_lease import run_periodic_maintenance
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.errors import ProviderReauthorizationRequired
from cognis.providers.llm.monitoring import (
    as_utc,
    monitoring_options,
    provider_fingerprint,
    usage_severity,
)
from cognis.store.coordination import DatabaseLeaseStore, Lease, database_now
from cognis.store.models import (
    CoordinationLeaseRow,
    InboxNotificationRow,
    LLMProvider,
    ProviderMonitorSnapshot,
    User,
)

if TYPE_CHECKING:
    from cognis.core.inbox import InboxService

REFRESH_TIMEOUT_SECONDS = 25
REAUTHORIZATION_REQUIRED_ERROR = "Reauthorization required"
logger = logging.getLogger(__name__)


class MonitorSource(Protocol):
    async def get_provider_usage(self, provider_id: str) -> dict[str, Any]: ...
    async def check_provider_connectivity(self, provider: LLMProvider) -> dict[str, Any]: ...


def refresh_deadline(provider: LLMProvider, snapshot: ProviderMonitorSnapshot) -> datetime:
    """Apply current scheduling options without invalidating useful observations."""
    interval = monitoring_options(provider).interval_minutes * 60
    delay = min(interval * 2 ** min(snapshot.failures, 3), 86400) if snapshot.failures else interval
    return as_utc(snapshot.checked_at) + timedelta(seconds=delay)


def project_monitor(
    provider: LLMProvider, snapshot: ProviderMonitorSnapshot | None, *, now: datetime
) -> ProviderMonitorResponse:
    """Project only observations for the current provider configuration."""
    options = monitoring_options(provider)
    result = ProviderMonitorResponse(
        provider_id=provider.provider_id, display_name=provider.display_name, monitoring=options
    )
    if snapshot is None or snapshot.fingerprint != provider_fingerprint(provider):
        return result
    result.health_status = snapshot.health.get("status", "unknown")
    result.health_detail = snapshot.health.get("detail", "Not checked")
    result.health_checked_at = (
        as_utc(snapshot.health_checked_at) if snapshot.health_checked_at else None
    )
    result.checked_at = as_utc(snapshot.checked_at)
    result.next_refresh_at = refresh_deadline(provider, snapshot)
    result.error = snapshot.error
    result.stale = now >= result.next_refresh_at or bool(snapshot.error)
    if snapshot.usage is not None:
        result.usage = ProviderUsageResponse.model_validate(
            {**snapshot.usage, "provider_id": provider.provider_id}
        )
    quota = usage_severity(snapshot.usage, options, now=now)
    if quota in {"critical", "warning"}:
        result.alert_severity = quota
    if result.health_status == "error":
        result.alert_severity = "critical"
    elif (
        result.health_status == "warning" or snapshot.failures >= 2
    ) and not result.alert_severity:
        result.alert_severity = "warning"
    return result


class ProviderMonitor:
    """Lifecycle-managed, shared monitor with bounded non-generating checks."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: MonitorSource,
        *,
        usage_observer: Callable[[str, str, str, str, datetime, ProviderUsageResponse], None]
        | None = None,
        inbox: InboxService | None = None,
    ) -> None:
        self.sessions = session_factory
        self.source = source
        self.usage_observer = usage_observer
        self.inbox = inbox
        self.leases = DatabaseLeaseStore(session_factory)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._reauthorization_checks: dict[str, asyncio.Task[None]] = {}
        self._concurrency = asyncio.Semaphore(2)

    async def start(self) -> None:
        """Start one periodic owner across replicas."""
        self._stop.clear()
        self._task = asyncio.create_task(
            run_periodic_maintenance(
                self.check_due,
                stop=self._stop,
                interval_seconds=30,
                resource_key="provider-monitor-scheduler",
                lease_store=self.leases,
            ),
            name="provider-monitor",
        )

    async def stop(self) -> None:
        """Cancel pending checks and release ownership."""
        self._stop.set()
        pending = [*self._reauthorization_checks.values()]
        if self._task:
            pending.append(self._task)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reauthorization_checks.clear()
        self._task = None

    def report_reauthorization_required(self, provider_id: str) -> None:
        """Publish a rejected credential now instead of at the next scheduled check.

        Inference failures call this synchronously. The check itself runs in the
        background through the normal lease-coalesced refresh, so a report made
        during the monitor's own check reuses that check's result.
        """
        if self._stop.is_set() or provider_id in self._reauthorization_checks:
            return
        task = asyncio.create_task(
            self._check_reauthorization(provider_id),
            name=f"provider-monitor-reauthorization:{provider_id}",
        )
        self._reauthorization_checks[provider_id] = task
        task.add_done_callback(lambda _: self._reauthorization_checks.pop(provider_id, None))

    async def _check_reauthorization(self, provider_id: str) -> None:
        _, snapshot, _ = await self._read(provider_id)
        if snapshot is not None and snapshot.error == REAUTHORIZATION_REQUIRED_ERROR:
            return
        with contextlib.suppress(ValueError, TimeoutError):
            await self.get(provider_id, force=True)

    async def list_visible(self, user_email: str) -> list[ProviderMonitorResponse]:
        """Read shared snapshots, never fetch upstream from a list request."""
        async with self.sessions() as session:
            now = await database_now(session)
            rows = (
                await session.execute(
                    select(LLMProvider, ProviderMonitorSnapshot)
                    .outerjoin(ProviderMonitorSnapshot)
                    .where(
                        LLMProvider.status == "active",
                        LLMProvider.owner_email.in_([user_email, SYSTEM_USER_EMAIL]),
                    )
                    .order_by(LLMProvider.display_name, LLMProvider.provider_id)
                )
            ).all()
            return [project_monitor(provider, snapshot, now=now) for provider, snapshot in rows]

    async def _read(
        self, provider_id: str
    ) -> tuple[LLMProvider, ProviderMonitorSnapshot | None, datetime]:
        async with self.sessions() as session:
            provider = await session.get(LLMProvider, provider_id)
            if provider is None:
                raise ValueError("LLM provider not found")
            snapshot = await session.get(ProviderMonitorSnapshot, provider_id)
            now = await database_now(session)
        if snapshot and snapshot.fingerprint != provider_fingerprint(provider):
            snapshot = None
        return provider, snapshot, now

    async def get(
        self, provider_id: str, *, force: bool = False, refresh_if_due: bool = True
    ) -> ProviderMonitorResponse:
        """Reuse fresh data, or coalesce a due/manual refresh across replicas."""
        provider, snapshot, requested_at = await self._read(provider_id)
        options = monitoring_options(provider)
        initial_revision = snapshot.revision if snapshot else None
        due = snapshot is None or refresh_deadline(provider, snapshot) <= requested_at
        if provider.status != "active" or not (
            force or (refresh_if_due and options.enabled and due)
        ):
            return project_monitor(provider, snapshot, now=requested_at)
        # The budget includes queueing, not only time spent owning a slot.
        async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
            async with self._concurrency:
                while True:
                    lease = await self.leases.acquire(
                        f"provider-monitor:{provider_id}", uuid.uuid4().hex, ttl_seconds=45
                    )
                    if lease:
                        try:
                            provider, snapshot, now = await self._read(provider_id)
                            options = monitoring_options(provider)
                            if provider.status != "active":
                                return project_monitor(provider, snapshot, now=now)
                            # A concurrent caller completed this same refresh, including force requests.
                            completed = snapshot and snapshot.revision != initial_revision
                            fresh = snapshot and refresh_deadline(provider, snapshot) > now
                            if completed or (not force and (fresh or not options.enabled)):
                                return project_monitor(provider, snapshot, now=now)
                            await self._check(provider, snapshot, lease)
                            provider, snapshot, now = await self._read(provider_id)
                            return project_monitor(provider, snapshot, now=now)
                        finally:
                            await self.leases.release(lease)
                    await asyncio.sleep(0.1)

    async def _check(
        self, provider: LLMProvider, previous: ProviderMonitorSnapshot | None, lease: Lease
    ) -> None:
        fingerprint = provider_fingerprint(provider)
        health = previous.health if previous else {"status": "unknown", "detail": "Not checked"}
        health_at = previous.health_checked_at if previous else None
        usage = previous.usage if previous else None
        fresh_usage: ProviderUsageResponse | None = None
        error = None
        failures = 0
        try:
            async with asyncio.timeout(18):
                report = await self.source.get_provider_usage(provider.provider_id)
                # Persist only the public response contract, never arbitrary upstream payloads.
                report = ProviderUsageResponse.model_validate(
                    {**report, "provider_id": provider.provider_id}
                ).model_dump(mode="json", exclude={"provider_id"})
                if report["ok"]:
                    fresh_usage = ProviderUsageResponse.model_validate(
                        {**report, "provider_id": provider.provider_id}
                    )
                # Do not erase a shared header observation on a replica with no responses.
                if (
                    report["ok"]
                    or usage is None
                    or report["source"] == "elevenlabs_subscription_usage"
                ):
                    usage = report
                if (
                    report["source"]
                    in {
                        "chatgpt_codex_usage",
                        "anthropic_subscription_usage",
                        "elevenlabs_subscription_usage",
                    }
                    and report["ok"]
                ):
                    health = {
                        "status": "healthy",
                        "detail": "Usage endpoint reachable; no generation performed",
                    }
                else:
                    health = await self.source.check_provider_connectivity(provider)
                async with self.sessions() as session:
                    health_at = await database_now(session)
        except ProviderReauthorizationRequired as exc:
            failures = (previous.failures if previous else 0) + 1
            error = REAUTHORIZATION_REQUIRED_ERROR
            # The message is built by Cognis from the provider label, never upstream text.
            health = {"status": "error", "detail": str(exc)}
        except Exception as exc:
            failures = (previous.failures if previous else 0) + 1
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            # Upstream exception strings may contain URLs, credentials, or response bodies.
            error = (
                "Provider authentication rejected"
                if status in {401, 403}
                else "Provider check failed"
            )
            if status in {401, 403}:
                health = {"status": "error", "detail": error}
            elif status == 429:
                health = {"status": "warning", "detail": "Provider usage endpoint rate limited"}
        published = None
        notifications: list[InboxNotificationRow] = []
        async with self.sessions() as session:
            if not await self.leases.is_current_in_session(session, lease):
                return
            current = await session.scalar(
                select(LLMProvider)
                .where(LLMProvider.provider_id == provider.provider_id)
                .with_for_update()
            )
            if (
                current is None
                or current.status != "active"
                or provider_fingerprint(current) != fingerprint
            ):
                return
            now = await database_now(session)
            interval = monitoring_options(current).interval_minutes * 60
            delay = min(interval * 2 ** min(failures, 3), 86400) if failures else interval
            row = await session.get(ProviderMonitorSnapshot, provider.provider_id)
            if row is None:
                row = ProviderMonitorSnapshot(provider_id=provider.provider_id)
                session.add(row)
            row.fingerprint = fingerprint
            row.revision = lease.fencing_token
            row.checked_at = now
            row.next_refresh_at = now + timedelta(seconds=delay)
            row.health = health
            row.health_checked_at = health_at
            row.usage = usage
            if error == REAUTHORIZATION_REQUIRED_ERROR and (
                previous is None or previous.error != REAUTHORIZATION_REQUIRED_ERROR
            ):
                notifications = await self._stage_reauthorization_notifications(session, current)
            row.error = error
            row.failures = failures
            if fresh_usage is not None and current.owner_email is not None:
                published = (
                    current.provider_id,
                    current.owner_email,
                    fingerprint,
                    f"umonitor_{lease.fencing_token}",
                    now,
                    fresh_usage,
                )
            await session.commit()
        if self.inbox is not None:
            for notification in notifications:
                await self.inbox.publish(notification)
        if published is not None and self.usage_observer is not None:
            try:
                self.usage_observer(*published)
            except Exception as exc:
                logger.warning("Usage quota observer failed: %s", type(exc).__name__)

    @staticmethod
    async def _stage_reauthorization_notifications(
        session: AsyncSession, provider: LLMProvider
    ) -> list[InboxNotificationRow]:
        """Tell whoever can reauthorize: the owner, or admins for shared providers."""
        if provider.owner_email and provider.owner_email != SYSTEM_USER_EMAIL:
            recipients = [provider.owner_email]
        else:
            recipients = list(
                (
                    await session.scalars(
                        select(User.email)
                        .where(User.role == "admin", User.is_active.is_(True))
                        .order_by(User.email)
                    )
                ).all()
            )
        href = f"/settings?tab=providers&provider={quote(provider.provider_id, safe='')}"
        return [
            stage_notification(
                session,
                user_email=recipient,
                kind="provider.reauthorization_required",
                title=f"{provider.display_name} needs reauthorization",
                body=(
                    f"{provider.display_name} sign-in expired or was revoked. Requests that "
                    "use it fail until you reauthorize it in Settings > Providers."
                ),
                href=href,
            )
            for recipient in recipients
        ]

    async def check_due(self) -> None:
        """One bounded scheduler pass; user refreshes advance the same deadlines."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(LLMProvider, ProviderMonitorSnapshot)
                    .outerjoin(ProviderMonitorSnapshot)
                    .where(LLMProvider.status == "active")
                    .order_by(LLMProvider.provider_id)
                )
            ).all()
            now = await database_now(session)
            due = [
                provider.provider_id
                for provider, snapshot in rows
                if monitoring_options(provider).enabled
                and (
                    snapshot is None
                    or snapshot.fingerprint != provider_fingerprint(provider)
                    or refresh_deadline(provider, snapshot) <= now
                )
            ]
        for provider_id in due:
            if self._stop.is_set():
                return
            with contextlib.suppress(ValueError, TimeoutError):
                await self.get(provider_id)

    async def invalidate(self, provider_id: str) -> None:
        """Fence in-flight credential checks and discard obsolete observations."""
        async with self.sessions() as session:
            now = await database_now(session)
            await session.execute(
                update(CoordinationLeaseRow)
                .where(CoordinationLeaseRow.resource_key == f"provider-monitor:{provider_id}")
                .values(
                    fencing_token=CoordinationLeaseRow.fencing_token + 1,
                    lease_expires_at=now,
                )
            )
            await session.execute(
                delete(ProviderMonitorSnapshot).where(
                    ProviderMonitorSnapshot.provider_id == provider_id
                )
            )
            await session.commit()
