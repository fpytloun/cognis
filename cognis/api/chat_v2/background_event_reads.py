"""Controller-local admission and backoff for background event-store reads."""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, TypeVar

from prometheus_client import Counter, Gauge

_Result = TypeVar("_Result")

BACKGROUND_EVENT_READ_ADMISSION = Counter(
    "cognis_background_event_read_admission_total",
    "Controller-local background event-read admission outcomes.",
    ["outcome"],
)
BACKGROUND_EVENT_READ_BACKOFF_SECONDS = Gauge(
    "cognis_background_event_read_backoff_seconds",
    "Current controller-local delay before the next background event-read probe.",
)


class BackgroundEventReadAdmission:
    """Bound concurrent background reads and back off after provider failures."""

    def __init__(
        self,
        *,
        max_concurrency: int = 2,
        initial_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        jitter_ratio: float = 0.2,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 16:
            raise ValueError("max_concurrency must be in 1..16")
        if initial_backoff_seconds <= 0:
            raise ValueError("initial_backoff_seconds must be positive")
        if max_backoff_seconds < initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must not be less than the initial backoff")
        if not 0 <= jitter_ratio <= 0.5:
            raise ValueError("jitter_ratio must be between 0 and 0.5")
        self._slots = asyncio.Semaphore(max_concurrency)
        self._initial_backoff = initial_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._jitter_ratio = jitter_ratio
        self._clock = clock
        self._sleep = sleep
        self._random_value = random_value
        self._state_lock = asyncio.Lock()
        self._failure_count = 0
        self._failure_epoch = 0
        self._next_probe_at = 0.0
        self._probe_active = False

    def diagnostics(self) -> dict[str, int | float | bool]:
        """Return content-free process-local state."""

        return {
            "failure_count": self._failure_count,
            "next_probe_in_seconds": max(0.0, self._next_probe_at - self._clock()),
            "probe_active": self._probe_active,
        }

    async def run(self, operation: Callable[[], Awaitable[_Result]]) -> _Result:
        """Run one admitted background event read."""

        probe, failure_epoch = await self._acquire()
        try:
            result = await operation()
        except asyncio.CancelledError:
            await self._release_cancelled(probe)
            raise
        except BaseException:
            await self._release_failed(probe)
            raise
        await self._release_succeeded(probe, failure_epoch)
        return result

    async def _acquire(self) -> tuple[bool, int]:
        while True:
            await self._slots.acquire()
            try:
                delay = 0.0
                probe = False
                async with self._state_lock:
                    now = self._clock()
                    failure_epoch = self._failure_epoch
                    if self._failure_count == 0:
                        self._metric("admitted")
                        return False, failure_epoch
                    if now >= self._next_probe_at and not self._probe_active:
                        self._probe_active = True
                        probe = True
                    else:
                        delay = max(0.01, self._next_probe_at - now)
                        if self._probe_active:
                            delay = min(delay, 0.05)
            except BaseException:
                self._slots.release()
                raise
            if probe:
                self._metric("probe")
                return True, failure_epoch
            self._slots.release()
            self._metric("backed_off")
            await self._sleep(delay)

    async def _release_cancelled(self, probe: bool) -> None:
        async def update() -> None:
            async with self._state_lock:
                if probe:
                    self._probe_active = False

        await self._release_permit_after(update)

    async def _release_failed(self, probe: bool) -> None:
        async def update() -> None:
            async with self._state_lock:
                self._failure_count += 1
                self._failure_epoch += 1
                base = min(
                    self._max_backoff,
                    self._initial_backoff * (2 ** (self._failure_count - 1)),
                )
                jitter = base * self._jitter_ratio * ((2 * self._random_value()) - 1)
                delay = max(0.01, min(self._max_backoff, base + jitter))
                self._next_probe_at = self._clock() + delay
                if probe:
                    self._probe_active = False
                with contextlib.suppress(Exception):
                    BACKGROUND_EVENT_READ_BACKOFF_SECONDS.set(delay)

        await self._release_permit_after(update)
        self._metric("failure")

    async def _release_succeeded(self, probe: bool, failure_epoch: int) -> None:
        recovered = False

        async def update() -> None:
            nonlocal recovered
            async with self._state_lock:
                had_failures = self._failure_count > 0
                if had_failures and (not probe or failure_epoch != self._failure_epoch):
                    return
                recovered = had_failures
                self._failure_count = 0
                self._next_probe_at = 0.0
                self._probe_active = False
                with contextlib.suppress(Exception):
                    BACKGROUND_EVENT_READ_BACKOFF_SECONDS.set(0)

        await self._release_permit_after(update)
        if recovered:
            self._metric("recovered")

    async def _release_permit_after(
        self,
        update: Callable[[], Awaitable[None]],
    ) -> None:
        """Complete one state transition before releasing exactly one permit."""

        transition: asyncio.Future[None] = asyncio.ensure_future(update())
        cancelled = False
        try:
            while not transition.done():
                try:
                    await asyncio.shield(transition)
                except asyncio.CancelledError:
                    cancelled = True
            await transition
        finally:
            self._slots.release()
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _metric(outcome: str) -> None:
        with contextlib.suppress(Exception):
            BACKGROUND_EVENT_READ_ADMISSION.labels(outcome=outcome).inc()


class AdmittedSessionEventStore:
    """Apply background admission to one already-authorized event-store reader."""

    def __init__(self, delegate: Any, admission: BackgroundEventReadAdmission) -> None:
        self._delegate = delegate
        self._admission = admission
        self.store_id = delegate.store_id

    async def read_session_events(self, **kwargs: Any) -> Any:
        return await self._admission.run(lambda: self._delegate.read_session_events(**kwargs))

    async def read_session_high_watermark(self, **kwargs: Any) -> Any:
        return await self._admission.run(
            lambda: self._delegate.read_session_high_watermark(**kwargs)
        )


__all__ = [
    "AdmittedSessionEventStore",
    "BackgroundEventReadAdmission",
]
