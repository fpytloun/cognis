"""Bounded async retry queue for failed Mnemory remember() calls."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from prometheus_client import Counter, Gauge

from cognis.logging import get_logger

logger = get_logger(__name__)

QUEUE_DEPTH = Gauge("cognis_remember_queue_depth", "Current remember queue depth")
QUEUE_DROPPED = Counter("cognis_remember_queue_dropped_total", "Dropped remember queue items")
QUEUE_FAILED = Counter("cognis_remember_queue_failed_total", "Failed remember queue items")
QUEUE_SUCCESS = Counter("cognis_remember_queue_success_total", "Successful remember queue items")


@dataclass
class RememberQueueItem:
    payload: dict[str, Any]
    attempts: int = 0
    next_retry_at: float = field(default_factory=monotonic)


class RememberRetryQueue:
    """Bounded in-memory retry queue with async drain workers."""

    def __init__(self, worker: Any, max_depth: int = 100, max_concurrent: int = 5) -> None:
        self.worker = worker
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.max_retries = 5
        self.backoff_max = 60.0
        self._items: deque[RememberQueueItem] = deque()
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()

    async def enqueue(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._items) >= self.max_depth:
                self._items.popleft()
                QUEUE_DROPPED.inc()
                logger.warning("Remember queue overflow; dropped oldest item")
            self._items.append(RememberQueueItem(payload=payload))
            QUEUE_DEPTH.set(len(self._items))

    async def _drain_loop(self) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        while not self._stop_event.is_set() or self._items:
            await asyncio.sleep(0.1)
            ready: list[RememberQueueItem] = []
            async with self._lock:
                now = monotonic()
                remaining: deque[RememberQueueItem] = deque()
                while self._items:
                    item = self._items.popleft()
                    if item.next_retry_at <= now:
                        ready.append(item)
                    else:
                        remaining.append(item)
                self._items = remaining
                QUEUE_DEPTH.set(len(self._items))
            if not ready:
                continue
            await asyncio.gather(*(self._process(item, semaphore) for item in ready))

    async def _process(self, item: RememberQueueItem, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                await self.worker.remember(**item.payload)
                QUEUE_SUCCESS.inc()
            except Exception:
                item.attempts += 1
                if item.attempts >= self.max_retries:
                    QUEUE_FAILED.inc()
                    logger.warning("Remember queue item failed permanently")
                    return
                item.next_retry_at = monotonic() + min(2**item.attempts, self.backoff_max)
                async with self._lock:
                    self._items.append(item)
                    QUEUE_DEPTH.set(len(self._items))
