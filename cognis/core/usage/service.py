"""Bounded nonblocking capture and one pending-only canonical batch writer.

State: controller lifetime queue and loss counters; DB facts/outbox are durable.
Cancellation: service owns writer cancellation/drain, never inference cancellation.
Idempotency: frozen event digests plus set-based locked content verification.
Raw transactions never read pricing/projection state or write a summary.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.usage.capture_models import (
    UsageDrainResult,
    UsageProviderSample,
    UsageWriterLimits,
)
from cognis.models.usage import (
    USAGE_OBSERVATION_MAX_BYTES,
    UsageAttemptFinalized,
    UsageAttemptStarted,
    UsageObservation,
    UsageRequestStats,
)
from cognis.store.usage.codec import context_values, terminal_values
from cognis.store.usage.models import (
    ProviderUsageSampleRow,
    UsageAttemptRow,
    UsageCoverageEpochRow,
    UsageProjectionOutboxRow,
    UsageRequestStatsRow,
)
from cognis.store.usage.transactions import (
    insert_for,
    notify_usage,
    row_values,
    sqlite_write_boundary,
)

logger = logging.getLogger(__name__)
CaptureItem = UsageObservation | UsageProviderSample
MAX_COUNTER = 2**63 - 1


class UsageAccountingService:
    """The synchronous sink accepts memory only; durability is deliberately asynchronous."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        controller_owner_id: str,
        projection_waker: Callable[[], None],
        limits: UsageWriterLimits | None = None,
        clock: Callable[[], datetime] | None = None,
        capture_gap: Callable[[], bool] | None = None,
    ) -> None:
        self._sessions = session_factory
        self.controller_owner_id = controller_owner_id
        self.coverage_epoch_id = f"uce_{uuid.uuid4().hex}"
        self._limits = limits or UsageWriterLimits()
        self._wake_projection = projection_waker
        self._queue: deque[tuple[CaptureItem, int]] = deque()
        self._bytes = 0
        self._inflight: list[CaptureItem] = []
        self._inflight_bytes = 0
        self._ready = asyncio.Event()
        self._empty = asyncio.Event()
        self._empty.set()
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self._dropped = [0, 0, 0, 0]
        self._batches = 0
        self._payload_bytes = 0
        self._metadata_reduced_events = 0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started_at = self._clock()
        self._writer_failed = False
        self._closing = False
        self._capture_gap = capture_gap
        self._final_task: asyncio.Task[None] | None = None
        self._epoch_closed = False

    @property
    def replay_window_days(self) -> int:
        """Missing-event replay bound; activated detail retention cannot be shorter."""
        return self._limits.replay_days

    async def start(self) -> None:
        """Start exactly one writer; startup itself does not await accounting SQL."""
        if self._closing:
            raise RuntimeError("Usage writer is closed")
        if self._task is None:
            self._accepting = True
            self._empty.clear()
            self._task = asyncio.create_task(self._run(), name="usage-canonical-writer")
            self._task.add_done_callback(self._writer_done)

    def _writer_done(self, task: asyncio.Task[None]) -> None:
        error = None if task.cancelled() else task.exception()
        if error is not None or not self._closing:
            self._writer_failed = True
            self._accepting = False
            for item, _ in self._queue:
                self._drop(item)
            for item in self._inflight:
                self._drop(item)
            self._queue.clear()
            self._inflight.clear()
            self._inflight_bytes = 0
            self._bytes = 0
            self._empty.set()
            logger.warning(
                "Usage writer stopped; error_class=%s",
                type(error).__name__ if error else "UnexpectedStop",
            )

    def try_emit(self, observation: UsageObservation) -> bool:
        """Accept bounded memory only; telemetry failure cannot escape into inference."""
        accepted = self._enqueue(observation)
        if accepted:
            reduction = (
                observation.metadata_reduction
                if isinstance(observation, UsageRequestStats)
                else observation.context.metadata_reduction
            )
            if reduction is not None:
                self._metadata_reduced_events = min(MAX_COUNTER, self._metadata_reduced_events + 1)
        return accepted

    def try_emit_provider_sample(self, observation: UsageProviderSample) -> bool:
        """Participant hook for existing provider refresh/header paths, not a sampler."""
        return self._enqueue(observation)

    def _drop(self, item: CaptureItem) -> None:
        index = (
            0
            if isinstance(item, UsageAttemptStarted)
            else 1
            if isinstance(item, UsageAttemptFinalized)
            else 2
            if isinstance(item, UsageRequestStats)
            else 3
        )
        self._dropped[index] = min(MAX_COUNTER, self._dropped[index] + 1)

    def _enqueue(self, item: CaptureItem) -> bool:
        try:
            size = len(item.model_dump_json(exclude_none=True, exclude_defaults=True).encode())
            if (
                not self._accepting
                or self._task is None
                or self._task.done()
                or size > USAGE_OBSERVATION_MAX_BYTES
                or len(self._queue) + len(self._inflight) >= self._limits.queue_items
                or self._bytes + self._inflight_bytes + size > self._limits.queue_bytes
            ):
                self._drop(item)
                return False
            self._queue.append((item, size))
            self._bytes += size
            self._empty.clear()
            if self._timer is None:
                self._timer = asyncio.get_running_loop().call_later(
                    self._limits.flush_seconds, self._ready.set
                )
            if len(self._queue) >= self._limits.batch_items:
                self._ready.set()
            return True
        except Exception:
            # This is the intentional telemetry isolation boundary. Never log the item.
            self._drop(item)
            return False

    def diagnostics(self) -> UsageDrainResult:
        """Expose fixed-cardinality coverage counters without identifiers/payloads."""
        return UsageDrainResult(
            len(self._queue) + len(self._inflight),
            self._bytes + self._inflight_bytes,
            self._dropped[0],
            self._dropped[1],
            self._dropped[2],
            self._dropped[3],
            self._batches,
            self._payload_bytes,
            self._metadata_reduced_events,
            self._writer_failed,
            self._task is not None and not self._task.done(),
            self._closing
            and (
                not self._epoch_closed
                or any(
                    task is not None and not task.done() for task in (self._task, self._final_task)
                )
            ),
        )

    async def drain(self, *, timeout_seconds: float = 5.0) -> UsageDrainResult:
        """Explicit lifecycle/test barrier, never called on inference admission."""
        self._ready.set()
        with suppress(TimeoutError):
            await asyncio.wait_for(self._empty.wait(), timeout_seconds)
        return self.diagnostics()

    async def aclose(self) -> None:
        """Bound each close/join attempt; retain ownership when cleanup outlives it."""
        self._accepting = False
        was_closing = self._closing
        self._closing = True
        deadline = asyncio.get_running_loop().time() + self._limits.shutdown_seconds
        if not was_closing:
            await self.drain(timeout_seconds=self._limits.shutdown_seconds * 0.8)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._task is not None:
            if not self._task.done() and not self._task.cancelling():
                self._task.cancel()
            done, _ = await asyncio.wait(
                {self._task},
                timeout=max(0, deadline - asyncio.get_running_loop().time()) * 0.5,
            )
            if not done:
                self._writer_failed = True
                logger.warning("Usage writer shutdown join exceeded its bounded budget")
                return
            self._task = None
        for item, _size in self._queue:
            self._drop(item)
        for item in self._inflight:
            self._drop(item)
        self._queue.clear()
        self._inflight.clear()
        self._inflight_bytes = 0
        self._bytes = 0
        if self._epoch_closed:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            if self._final_task is None or self._final_task.done():
                self._final_task = asyncio.create_task(
                    self._close_epoch(), name="usage-final-coverage"
                )
                self._final_task.add_done_callback(self._consume_final)
            final = self._final_task
            done, _ = await asyncio.wait({final}, timeout=remaining)
            if not done:
                self._writer_failed = True
                if not final.cancelling():
                    final.cancel()
                logger.warning("Usage final coverage exceeded bounded shutdown")
        else:
            self._writer_failed = True
            logger.warning("Usage final coverage skipped after bounded shutdown")

    async def _close_epoch(self) -> None:
        try:
            await self._persist_epoch(closed=True)
            self._epoch_closed = True
        except Exception as exc:
            self._writer_failed = True
            logger.warning("Usage epoch close failed: %s", type(exc).__name__)

    async def _persist_epoch(self, *, closed: bool = False) -> None:
        async with self._sessions.begin() as session:
            await self._record_epoch(session, closed=closed)

    @staticmethod
    def _consume_final(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _record_epoch(self, session: AsyncSession, *, closed: bool = False) -> None:
        gap = any(self._dropped) or self._metadata_reduced_events > 0 or self._writer_failed
        if self._capture_gap is not None:
            gap |= self._capture_gap()
        values = {
            "epoch_id": self.coverage_epoch_id,
            "controller_owner_id": self.controller_owner_id,
            "started_at": self._started_at,
            "ended_at": self._clock() if closed else None,
            "dropped_started": self._dropped[0],
            "dropped_finalized": self._dropped[1],
            "dropped_stats": min(MAX_COUNTER, self._dropped[2] + self._dropped[3]),
            "metadata_reduced_events": self._metadata_reduced_events,
            "coverage_status": "gapped" if gap else "complete" if closed else "open",
        }
        statement = insert_for(session, UsageCoverageEpochRow.__table__).values(values)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["epoch_id"],
                set_={key: value for key, value in values.items() if key != "epoch_id"},
            )
        )

    async def _run(self) -> None:
        # Establish crash-visible coverage even before the first observation.
        try:
            await self._persist_epoch()
        except SQLAlchemyError as exc:
            logger.warning("Usage initial coverage failed: %s", type(exc).__name__)
        if not self._queue:
            self._empty.set()
        while True:
            await self._ready.wait()
            self._ready.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            while self._queue:
                batch: list[CaptureItem] = []
                size = 0
                while self._queue and len(batch) < self._limits.batch_items:
                    item, item_size = self._queue[0]
                    if batch and size + item_size > self._limits.batch_bytes:
                        break
                    self._queue.popleft()
                    self._bytes -= item_size
                    size += item_size
                    batch.append(item)
                batch_id = f"ucb_{uuid.uuid4().hex}"
                self._inflight = batch
                self._inflight_bytes = size
                for attempt in range(self._limits.db_attempts):
                    try:
                        await self._commit_batch(batch, batch_id)
                        self._batches += 1
                        self._payload_bytes += size
                        # The durable outbox remains recoverable if local wake fails.
                        try:
                            self._wake_projection()
                        except Exception:
                            logger.warning("Usage local projection wake failed")
                        break
                    except SQLAlchemyError as exc:
                        logger.warning("Usage canonical batch failed: %s", type(exc).__name__)
                        if attempt + 1 == self._limits.db_attempts:
                            for item in batch:
                                self._drop(item)
                        else:
                            # Bounded retry exists only while this captured batch is pending.
                            await asyncio.sleep(0.025 * (attempt + 1))
                self._inflight = []
                self._inflight_bytes = 0
                await asyncio.sleep(0)
            self._empty.set()

    async def _commit_batch(self, items: list[CaptureItem], batch_id: str) -> None:
        now = self._clock()
        async with self._sessions.begin() as session:
            await sqlite_write_boundary(session)
            refs = await self._attempts(session, items, now)
            refs.extend(await self._requests(session, items, now))
            await self._samples(session, items)
            await self._record_epoch(session)
            if refs:
                statement = insert_for(session, UsageProjectionOutboxRow.__table__).values(
                    batch_id=batch_id,
                    controller_owner_id=self.controller_owner_id,
                    item_refs=refs,
                    created_at=now,
                    cursor=0,
                    claim_epoch=0,
                )
                await session.execute(statement.on_conflict_do_nothing(index_elements=["batch_id"]))
            if refs or any(isinstance(item, UsageProviderSample) for item in items):
                await notify_usage(session, self.controller_owner_id, batch_id)

    async def _attempts(
        self,
        session: AsyncSession,
        items: Sequence[CaptureItem],
        now: datetime,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[UsageAttemptStarted | UsageAttemptFinalized]] = {}
        for item in items:
            if isinstance(item, (UsageAttemptStarted, UsageAttemptFinalized)):
                grouped.setdefault(item.context.trace.attempt_id, []).append(item)
        if not grouped:
            return []
        # Lock in stable order. SQLite already owns its short writer boundary.
        existing = {
            row.attempt_id: row
            for row in (
                await session.scalars(
                    select(UsageAttemptRow)
                    .where(UsageAttemptRow.attempt_id.in_(sorted(grouped)))
                    .order_by(UsageAttemptRow.attempt_id)
                    .with_for_update()
                )
            ).all()
        }
        cutoff = now - timedelta(days=self._limits.replay_days)
        inserts = []
        updates = []
        refs = []
        for attempt_id, events in sorted(grouped.items()):
            old = existing.get(attempt_id)
            context = events[0].context
            if old is None and context.started_at < cutoff:
                for event in events:
                    self._drop(event)
                continue
            values = row_values(old) if old else context_values(context, now)
            changed = False
            for event in events:
                if values["context_digest"] != event.context.observation_digest():
                    self._drop(event)
                    continue
                if isinstance(event, UsageAttemptStarted):
                    if values.get("start_digest") not in (None, event.observation_digest()):
                        self._drop(event)
                        continue
                    if not values["start_observed"]:
                        values.update(
                            start_observed=True,
                            start_observation_id=event.observation_id,
                            start_digest=event.observation_digest(),
                        )
                        changed = True
                elif values.get("terminal_digest") is None:
                    values.update(terminal_values(event, now), fact_version=1)
                    changed = True
                elif values["terminal_digest"] != event.observation_digest():
                    self._drop(event)
            if old is None:
                inserts.append(values)
            elif changed:
                updates.append(values)
            if changed or old is None:
                refs.append(
                    {"kind": "attempt", "id": attempt_id, "version": values["fact_version"]}
                )
        table = UsageAttemptRow.__table__
        if inserts:
            statement = insert_for(session, table).values(inserts)
            result = await session.scalars(
                statement.on_conflict_do_nothing(index_elements=["attempt_id"]).returning(
                    table.c.attempt_id
                )
            )
            inserted = set(result.all())
            conflicts = set(v["attempt_id"] for v in inserts) - inserted
            if conflicts:
                # Concurrent inserts must be compared after the unique-key wait.
                # Re-enter once with the now-visible rows; no per-record lookup.
                conflict_items = [e for key in conflicts for e in grouped[key]]
                refs = [ref for ref in refs if ref["id"] not in conflicts]
                refs.extend(await self._attempts(session, conflict_items, now))
        if updates:
            statement = insert_for(session, table).values(updates)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["attempt_id"],
                    set_={
                        column.name: getattr(statement.excluded, column.name)
                        for column in table.columns
                        if column.name not in {"row_id", "attempt_id", "registered_at"}
                    },
                )
            )
        return refs

    async def _requests(
        self,
        session: AsyncSession,
        items: Sequence[CaptureItem],
        now: datetime,
    ) -> list[dict[str, Any]]:
        events = [item for item in items if isinstance(item, UsageRequestStats)]
        if not events:
            return []
        ids = sorted({item.request_id for item in events})
        existing = {
            row.request_id: row
            for row in (
                await session.scalars(
                    select(UsageRequestStatsRow)
                    .where(UsageRequestStatsRow.request_id.in_(ids))
                    .order_by(UsageRequestStatsRow.request_id)
                    .with_for_update()
                )
            ).all()
        }
        table = UsageRequestStatsRow.__table__
        changed: dict[str, dict[str, Any]] = {}
        for item in events:
            old = existing.get(item.request_id)
            if old is None and item.request_started_at < now - timedelta(
                days=self._limits.replay_days
            ):
                self._drop(item)
                continue
            identity = item.attribution.identity
            values = changed.get(item.request_id)
            if values is None:
                values = (
                    row_values(old)
                    if old
                    else {column.name: None for column in table.columns if column.nullable}
                    | {
                        "request_id": item.request_id,
                        "cycle_id": item.cycle_id,
                        "attempt_id": item.attempt_id,
                        "request_started_at": item.request_started_at,
                        "owner_key": identity.resource_owner or identity.usage_subject_key,
                        "usage_subject_key": identity.usage_subject_key,
                        "bucket_day": item.request_started_at.date(),
                        "attribution": item.attribution.model_dump(mode="json", exclude_none=True),
                        "controller_owner_id": item.controller_owner_id,
                        "coverage_epoch_id": item.coverage_epoch_id,
                        "fact_version": 0,
                        "projected_version": 0,
                    }
                )
            if values["attribution"] != item.attribution.model_dump(mode="json", exclude_none=True):
                self._drop(item)
                continue
            scope = item.stats_scope
            digest = item.observation_digest()
            previous = values.get(f"{scope}_digest")
            if previous is not None:
                if previous != digest:
                    self._drop(item)
                continue
            values[f"{scope}_digest"] = digest
            values[f"{scope}_observation_id"] = item.observation_id
            values[f"{scope}_payload"] = item.model_dump(
                mode="json", exclude={"attribution"}, exclude_none=True, exclude_defaults=True
            )
            span = item.server if scope == "server" else item.client
            if span is not None:
                values[f"{scope}_elapsed_ms"] = span.elapsed_ms
                values[f"{scope}_ttft_ms"] = span.ttft_ms
            if scope == "server":
                values["logical_outcome"] = item.logical_outcome
            values["fact_version"] += 1
            values["updated_at"] = now
            changed[item.request_id] = values
        refs = [
            {"kind": "request", "id": key, "version": value["fact_version"]}
            for key, value in changed.items()
        ]
        new = [value for key, value in sorted(changed.items()) if key not in existing]
        updates = [value for key, value in sorted(changed.items()) if key in existing]
        if new:
            statement = insert_for(session, table).values(new)
            inserted = set(
                (
                    await session.scalars(
                        statement.on_conflict_do_nothing(index_elements=["request_id"]).returning(
                            table.c.request_id
                        )
                    )
                ).all()
            )
            conflicts = {value["request_id"] for value in new} - inserted
            if conflicts:
                refs = [ref for ref in refs if ref["id"] not in conflicts]
                refs.extend(
                    await self._requests(
                        session, [item for item in events if item.request_id in conflicts], now
                    )
                )
        if updates:
            statement = insert_for(session, table).values(updates)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["request_id"],
                    set_={
                        column.name: getattr(statement.excluded, column.name)
                        for column in table.columns
                        if column.name != "request_id"
                    },
                )
            )
        return refs

    async def _samples(self, session: AsyncSession, items: list[CaptureItem]) -> None:
        samples = [item for item in items if isinstance(item, UsageProviderSample)]
        if not samples:
            return
        # Samples have stable IDs; same-batch replay is coalesced before one insert.
        values = {}
        for item in samples:
            if item.sample_id in values:
                continue
            values[item.sample_id] = {
                "sample_id": item.sample_id,
                "provider_id": item.provider_id,
                "provider_owner": item.provider_owner,
                "account_scope_id": item.account_scope_id,
                "configuration_generation": item.configuration_generation,
                "source": item.source,
                "observation_id": item.observation_id,
                "window_key": item.window_key,
                "model_bucket": item.model_bucket,
                "first_observed_at": item.observed_at,
                "last_observed_at": item.observed_at,
                "reset_at": item.reset_at,
                "limit_value": item.limit_value,
                "latest_remaining": item.remaining,
                "minimum_remaining": item.remaining,
                "maximum_utilization": item.utilization,
                "sample_count": item.sample_count,
                "content_digest": item.observation_digest(),
            }
        statement = insert_for(session, ProviderUsageSampleRow.__table__).values(
            list(values.values())
        )
        await session.execute(statement.on_conflict_do_nothing())
        stored = (
            await session.scalars(
                select(ProviderUsageSampleRow).where(ProviderUsageSampleRow.sample_id.in_(values))
            )
        ).all()
        digests = {row.sample_id: row.content_digest for row in stored}
        for item in samples:
            if digests.get(item.sample_id) != item.observation_digest():
                self._drop(item)
