"""Bounded background propagation for canonical event append invalidations."""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter, Gauge

from cognis.api.chat_v2.cached_event_store import AppendInvalidation
from cognis.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_PENDING_SESSIONS = 4096
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_INITIAL_SECONDS = 0.05
DEFAULT_BACKOFF_MAX_SECONDS = 1.0

APPEND_INVALIDATION_PENDING = Gauge(
    "cognis_chat_event_append_invalidation_pending",
    "Pending content-free canonical event append invalidations.",
)
APPEND_INVALIDATION_ENQUEUED = Counter(
    "cognis_chat_event_append_invalidation_enqueued_total",
    "Canonical event append invalidations admitted by the background dispatcher.",
)
APPEND_INVALIDATION_COALESCED = Counter(
    "cognis_chat_event_append_invalidation_coalesced_total",
    "Canonical event append invalidations coalesced by opaque session token.",
)
APPEND_INVALIDATION_PROCESSED = Counter(
    "cognis_chat_event_append_invalidation_processed_total",
    "Canonical event append invalidations propagated to shared stores.",
)
APPEND_INVALIDATION_RETRIED = Counter(
    "cognis_chat_event_append_invalidation_retried_total",
    "Canonical event append invalidation retries.",
)
APPEND_INVALIDATION_ERRORS = Counter(
    "cognis_chat_event_append_invalidation_errors_total",
    "Canonical event append invalidation processing failures.",
    labelnames=("stage",),
)
APPEND_INVALIDATION_DROPPED = Counter(
    "cognis_chat_event_append_invalidation_dropped_total",
    "Canonical event append invalidations dropped into TTL-bounded fallback.",
    labelnames=("reason",),
)


@dataclass(slots=True)
class _PendingInvalidation:
    work: AppendInvalidation
    attempts: int = 0
    cache_done: bool = False
    signal_done: bool = False


PublishInvalidation = Callable[[str, int], Awaitable[bool | None]]
CacheAdvanced = Callable[[AppendInvalidation], None]


class EventAppendInvalidationDispatcher:
    """Move Redis/PostgreSQL append propagation outside provider deadlines.

    Pending work is content-free and keyed by an opaque HMAC session token.
    Capacity eviction enables the event store's global direct-read fallback for
    the maximum canonical-cache TTL, bounding stale L1/L2 exposure to 30s.
    """

    def __init__(
        self,
        *,
        event_store: Any,
        publish_invalidation: PublishInvalidation,
        on_cache_advanced: CacheAdvanced | None = None,
        max_pending_sessions: int = DEFAULT_MAX_PENDING_SESSIONS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    ) -> None:
        if max_pending_sessions < 1 or max_pending_sessions > 4096:
            raise ValueError("max_pending_sessions must be in 1..4096")
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be in 1..10")
        if backoff_initial_seconds <= 0 or backoff_max_seconds < backoff_initial_seconds:
            raise ValueError("invalid append invalidation backoff bounds")
        self._event_store = event_store
        self._publish_invalidation = publish_invalidation
        self._on_cache_advanced = on_cache_advanced
        self._max_pending_sessions = max_pending_sessions
        self._max_attempts = max_attempts
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._pending: OrderedDict[str, _PendingInvalidation] = OrderedDict()
        self._available = asyncio.Event()
        self._stopping = False
        self._accepting = False
        self._worker: asyncio.Task[None] | None = None
        self._active: _PendingInvalidation | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._stopping = False
        self._accepting = True
        self._worker = asyncio.create_task(
            self._run(),
            name="event-append-invalidation-dispatcher",
        )

    def enqueue(self, work: AppendInvalidation) -> bool:
        """Synchronously enqueue opaque work without performing I/O."""

        if not self._accepting:
            self._drop(work, "not_accepting")
            return False
        existing = self._pending.pop(work.session_token, None)
        if existing is not None:
            existing.work = self._merge(existing.work, work)
            existing.cache_done = False
            existing.signal_done = False
            self._pending[work.session_token] = existing
            APPEND_INVALIDATION_COALESCED.inc()
        else:
            if len(self._pending) >= self._max_pending_sessions:
                _, evicted = self._pending.popitem(last=False)
                self._drop(evicted.work, "capacity")
            self._pending[work.session_token] = _PendingInvalidation(work=work)
            APPEND_INVALIDATION_ENQUEUED.inc()
        APPEND_INVALIDATION_PENDING.set(len(self._pending))
        self._available.set()
        return True

    async def stop(self, *, drain_timeout_seconds: float = 2.0) -> None:
        self._accepting = False
        self._stopping = True
        self._available.set()
        worker = self._worker
        if worker is None:
            self._drop_pending("shutdown")
            return
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=drain_timeout_seconds)
        except TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        finally:
            if self._active is not None:
                self._drop(self._active.work, "shutdown")
                self._active = None
            self._drop_pending("shutdown")
            self._worker = None
            APPEND_INVALIDATION_PENDING.set(0)

    async def _run(self) -> None:
        while not self._stopping or self._pending:
            item = self._pop()
            if item is None:
                self._available.clear()
                if self._pending:
                    self._available.set()
                    continue
                if self._stopping:
                    break
                await self._available.wait()
                continue
            self._active = item
            success = await self._process(item)
            if success:
                APPEND_INVALIDATION_PROCESSED.inc()
                self._active = None
                continue
            item.attempts += 1
            if item.attempts >= self._max_attempts:
                self._drop(item.work, "retries_exhausted")
                self._active = None
                continue
            APPEND_INVALIDATION_RETRIED.inc()
            delay = min(
                self._backoff_initial * (2 ** (item.attempts - 1)),
                self._backoff_max,
            )
            await asyncio.sleep(delay)
            self._requeue(item)
            self._active = None

    async def _process(self, item: _PendingInvalidation) -> bool:
        if not item.cache_done:
            try:
                item.cache_done = bool(
                    await self._event_store.process_append_invalidation(item.work)
                )
                if item.cache_done and self._on_cache_advanced is not None:
                    self._on_cache_advanced(item.work)
            except asyncio.CancelledError:
                raise
            except Exception:
                APPEND_INVALIDATION_ERRORS.labels(stage="cache").inc()
        if not item.signal_done:
            try:
                published = await self._publish_invalidation(
                    item.work.session_token,
                    item.work.last_seq,
                )
                item.signal_done = published is not False
            except asyncio.CancelledError:
                raise
            except Exception:
                APPEND_INVALIDATION_ERRORS.labels(stage="cluster_signal").inc()
        return item.cache_done and item.signal_done

    def _pop(self) -> _PendingInvalidation | None:
        if not self._pending:
            return None
        _, item = self._pending.popitem(last=False)
        APPEND_INVALIDATION_PENDING.set(len(self._pending))
        return item

    def _requeue(self, item: _PendingInvalidation) -> None:
        existing = self._pending.pop(item.work.session_token, None)
        if existing is not None:
            existing.work = self._merge(item.work, existing.work)
            existing.attempts = max(existing.attempts, item.attempts)
            existing.cache_done = existing.cache_done and item.cache_done
            existing.signal_done = existing.signal_done and item.signal_done
            item = existing
            APPEND_INVALIDATION_COALESCED.inc()
        elif len(self._pending) >= self._max_pending_sessions:
            _, evicted = self._pending.popitem(last=False)
            self._drop(evicted.work, "capacity")
        self._pending[item.work.session_token] = item
        APPEND_INVALIDATION_PENDING.set(len(self._pending))
        self._available.set()

    @staticmethod
    def _merge(current: AppendInvalidation, candidate: AppendInvalidation) -> AppendInvalidation:
        newest = candidate if candidate.local_revision >= current.local_revision else current
        return AppendInvalidation(
            session_token=current.session_token,
            authority_token=newest.authority_token,
            last_seq=max(current.last_seq, candidate.last_seq),
            has_events=current.has_events or candidate.has_events,
            local_revision=max(current.local_revision, candidate.local_revision),
        )

    def _drop_pending(self, reason: str) -> None:
        for item in self._pending.values():
            self._drop(item.work, reason)
        self._pending.clear()

    def _drop(self, work: AppendInvalidation, reason: str) -> None:
        with contextlib.suppress(Exception):
            self._event_store.abandon_append_invalidation(work)
        APPEND_INVALIDATION_DROPPED.labels(reason=reason).inc()
        logger.warning(
            "event append invalidation entered TTL-bounded fallback",
            extra={"extra_data": {"reason": reason}},
        )


__all__ = [
    "APPEND_INVALIDATION_COALESCED",
    "APPEND_INVALIDATION_DROPPED",
    "APPEND_INVALIDATION_ENQUEUED",
    "APPEND_INVALIDATION_ERRORS",
    "APPEND_INVALIDATION_PENDING",
    "APPEND_INVALIDATION_PROCESSED",
    "APPEND_INVALIDATION_RETRIED",
    "EventAppendInvalidationDispatcher",
]
