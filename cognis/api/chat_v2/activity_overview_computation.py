"""Bounded Activity Overview result cache and single-flight computation."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic

from prometheus_client import Counter

_RESULTS = Counter(
    "cognis_chat_activity_overview_computation_results_total",
    "Activity Overview computation result-cache outcomes.",
    ["outcome"],
)
_FLIGHTS = Counter(
    "cognis_chat_activity_overview_singleflight_total",
    "Activity Overview single-flight outcomes.",
    ["outcome"],
)
_WAITERS = Counter(
    "cognis_chat_activity_overview_waiters_total",
    "Activity Overview waiter outcomes.",
    ["outcome"],
)


def _metric(metric: Counter, outcome: str) -> None:
    try:
        metric.labels(outcome=outcome).inc()
    except Exception:
        return


@dataclass(frozen=True, slots=True)
class ActivityOverviewComputationKey:
    owner_email: str
    scope_key: str
    materializer_version: str
    projection_version: str
    graph_fingerprint: str
    graph_revision: int
    work_revision: int
    required_watermarks: tuple[tuple[str, int], ...] | None
    detail: str


@dataclass(slots=True)
class _CacheEntry:
    value: object
    expires_at: float


@dataclass(slots=True)
class _Flight:
    task: asyncio.Task[object]
    waiters: int = 0


class ActivityOverviewDisconnected(Exception):
    """The HTTP client disconnected while waiting for shared computation."""


class ActivityOverviewComputationService:
    """Share expensive overview aggregation within one controller process."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1 or max_entries > 4096:
            raise ValueError("max_entries must be in 1..4096")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: OrderedDict[ActivityOverviewComputationKey, _CacheEntry] = OrderedDict()
        self._flights: dict[ActivityOverviewComputationKey, _Flight] = {}
        self._lock = asyncio.Lock()
        self._accepting = True

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def flight_count(self) -> int:
        return len(self._flights)

    async def get_or_compute(
        self,
        key: ActivityOverviewComputationKey,
        loader: Callable[[], Awaitable[object]],
        *,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
        poll_seconds: float = 0.1,
    ) -> object:
        bypass = False
        async with self._lock:
            now = self._clock()
            entry = self._cache.pop(key, None)
            if entry is not None and entry.expires_at > now:
                self._cache[key] = entry
                _metric(_RESULTS, "hit")
                return deepcopy(entry.value)
            if entry is not None:
                _metric(_RESULTS, "expired")
            else:
                _metric(_RESULTS, "miss")
            flight = self._flights.get(key)
            if flight is None:
                if not self._accepting or len(self._flights) >= self._max_entries:
                    bypass = True
                else:
                    task = asyncio.create_task(self._compute(key, loader))
                    task.add_done_callback(self._consume_task_result)
                    flight = _Flight(task=task)
                    self._flights[key] = flight
                    _metric(_FLIGHTS, "created")
            else:
                _metric(_FLIGHTS, "joined")
            if flight is not None:
                flight.waiters += 1

        if bypass:
            return deepcopy(await loader())
        assert flight is not None

        try:
            while True:
                if disconnected is not None and await disconnected():
                    _metric(_WAITERS, "disconnected")
                    raise ActivityOverviewDisconnected
                try:
                    value = await asyncio.wait_for(
                        asyncio.shield(flight.task),
                        timeout=poll_seconds if disconnected is not None else None,
                    )
                    return deepcopy(value)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            _metric(_WAITERS, "cancelled")
            raise
        finally:
            async with self._lock:
                current = self._flights.get(key)
                if current is flight:
                    flight.waiters = max(0, flight.waiters - 1)
                    if flight.waiters == 0 and not flight.task.done():
                        self._flights.pop(key, None)
                        flight.task.cancel()
                        _metric(_FLIGHTS, "cancelled")

    async def stop(self) -> None:
        async with self._lock:
            self._accepting = False
            tasks = [flight.task for flight in self._flights.values()]
            for task in tasks:
                task.cancel()
            self._cache.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._flights.clear()

    async def _compute(
        self,
        key: ActivityOverviewComputationKey,
        loader: Callable[[], Awaitable[object]],
    ) -> object:
        try:
            value = deepcopy(await loader())
            async with self._lock:
                flight = self._flights.get(key)
                if flight is not None and flight.waiters > 0 and self._accepting:
                    self._cache[key] = _CacheEntry(
                        value=deepcopy(value),
                        expires_at=self._clock() + self._ttl_seconds,
                    )
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._max_entries:
                        self._cache.popitem(last=False)
                        _metric(_RESULTS, "evicted")
            _metric(_FLIGHTS, "succeeded")
            return value
        except asyncio.CancelledError:
            raise
        except Exception:
            _metric(_FLIGHTS, "failed")
            raise
        finally:
            async with self._lock:
                current = self._flights.get(key)
                if current is not None and current.task is asyncio.current_task():
                    self._flights.pop(key, None)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            return


__all__ = [
    "ActivityOverviewComputationKey",
    "ActivityOverviewComputationService",
    "ActivityOverviewDisconnected",
]
