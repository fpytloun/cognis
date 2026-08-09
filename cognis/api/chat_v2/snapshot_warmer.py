"""Bounded application-scoped canonical ChatSnapshot warming."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from time import monotonic
from typing import Literal

from cognis.api.chat_v2.snapshot_metrics import (
    SNAPSHOT_CACHE_METRICS,
    WARM_EVENTS,
    WARM_LAG,
    WarmFailureReason,
)
from cognis.logging import get_logger

logger = get_logger(__name__)
WarmStatus = Literal["succeeded", "skipped", "retry"]
WarmResult = tuple[WarmStatus, WarmFailureReason | None]
WarmCallback = Callable[[str], Awaitable[WarmResult]]
ActiveDiscovery = Callable[[], AsyncIterator[str]]


class ChatSnapshotWarmer:
    """Coalesce conversation requests while bounding concurrent rebuild work."""

    def __init__(
        self,
        callback: WarmCallback,
        *,
        worker_count: int = 4,
        clock: Callable[[], float] = monotonic,
        retry_seconds: float = 1.0,
        max_pending: int = 4096,
    ) -> None:
        if worker_count < 1 or worker_count > 16:
            raise ValueError("worker_count must be in 1..16")
        if retry_seconds <= 0:
            raise ValueError("retry_seconds must be positive")
        if max_pending < worker_count or max_pending > 4096:
            raise ValueError("max_pending must be between worker_count and 4096")
        self._callback = callback
        self._worker_count = worker_count
        self._clock = clock
        self._retry_seconds = retry_seconds
        self._max_pending = max_pending
        self._dirty: OrderedDict[str, float] = OrderedDict()
        self._active: set[str] = set()
        self._available = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._accepting = False
        self._stopping = False

    @property
    def pending_count(self) -> int:
        return len(self._dirty)

    def _update_gauges(self) -> None:
        SNAPSHOT_CACHE_METRICS.warmer(len(self._dirty), len(self._active))

    async def start(self, initial_conversation_ids: Iterable[str] = ()) -> None:
        if self._workers:
            return
        self._accepting = True
        self._stopping = False
        self._workers = [
            asyncio.create_task(self._run(index), name=f"chat-snapshot-warmer-{index}")
            for index in range(self._worker_count)
        ]
        for conversation_id in initial_conversation_ids:
            self.enqueue(conversation_id)

    def enqueue(self, conversation_id: str) -> bool:
        if not self._accepting or not conversation_id:
            return False
        WARM_EVENTS.labels(outcome="requested").inc()
        if conversation_id in self._dirty or conversation_id in self._active:
            WARM_EVENTS.labels(outcome="coalesced").inc()
        elif len(self._dirty) >= self._max_pending:
            SNAPSHOT_CACHE_METRICS.overflow("warmer")
            WARM_EVENTS.labels(outcome="overflow").inc()
            return False
        self._dirty.setdefault(conversation_id, self._clock())
        self._update_gauges()
        self._available.set()
        return True

    async def stop(self, *, drain_timeout_seconds: float = 2.0) -> None:
        self._accepting = False
        self._stopping = True
        self._available.set()
        workers = tuple(self._workers)
        if not workers:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers),
                timeout=drain_timeout_seconds,
            )
        except TimeoutError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self._workers.clear()
            self._dirty.clear()
            self._active.clear()
            self._update_gauges()

    async def _run(self, worker_index: int) -> None:
        del worker_index
        while not self._stopping or self._dirty:
            item = self._pop()
            if item is None:
                self._available.clear()
                if self._stopping and not self._dirty:
                    return
                await self._available.wait()
                continue
            conversation_id, requested_at = item
            self._active.add(conversation_id)
            self._update_gauges()
            result: WarmResult = ("retry", "internal")
            try:
                result = await self._callback(conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "chat_v2: snapshot warm callback failed",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                )
            finally:
                self._active.discard(conversation_id)
                self._update_gauges()
                if self._dirty:
                    self._available.set()
            status, reason = result
            if reason is not None:
                SNAPSHOT_CACHE_METRICS.warm_failure(reason)
            if status == "succeeded":
                WARM_EVENTS.labels(outcome="succeeded").inc()
                WARM_LAG.observe(max(0.0, self._clock() - requested_at))
            elif status == "skipped":
                WARM_EVENTS.labels(outcome="skipped").inc()
            else:
                WARM_EVENTS.labels(outcome="failed").inc()
                if reason is None:
                    SNAPSHOT_CACHE_METRICS.warm_failure("internal")
                await asyncio.sleep(self._retry_seconds)
                if not self._stopping:
                    self._dirty.setdefault(conversation_id, requested_at)
                    self._update_gauges()
                    self._available.set()

    def _pop(self) -> tuple[str, float] | None:
        for conversation_id in tuple(self._dirty):
            if conversation_id in self._active:
                continue
            requested_at = self._dirty.pop(conversation_id)
            self._update_gauges()
            return conversation_id, requested_at
        return None


class ChatSnapshotActiveReconciler:
    """Periodically enqueue every durable active conversation."""

    def __init__(
        self,
        discover: ActiveDiscovery,
        enqueue: Callable[[str], bool],
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._discover = discover
        self._enqueue = enqueue
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="chat-snapshot-active-reconciler",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def reconcile_once(self) -> None:
        async for conversation_id in self._discover():
            self._enqueue(conversation_id)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("chat_v2: active snapshot reconciliation failed")
