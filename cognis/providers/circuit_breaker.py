"""Reusable circuit breaker helper for provider calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(RuntimeError):
    """Raised when the circuit breaker is open."""


class CircuitBreaker:
    """Simple async circuit breaker."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        *,
        should_trip: Callable[[Exception], bool] | None = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.should_trip = should_trip
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at: datetime | None = None
        self._state_lock = asyncio.Lock()
        self._half_open_probe_active = False

    async def _allow_call(self) -> bool:
        async with self._state_lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if self.opened_at is None:
                    return False
                if datetime.now(UTC) - self.opened_at < timedelta(seconds=self.recovery_timeout):
                    return False
                self.state = CircuitState.HALF_OPEN
            if self._half_open_probe_active:
                return False
            self._half_open_probe_active = True
            return True

    async def call(self, func: Callable[[], Awaitable[T]]) -> T:
        """Run a protected async function."""
        if not await self._allow_call():
            raise CircuitBreakerError("Circuit breaker is open")
        try:
            result = await func()
        except BaseException as exc:
            should_trip = isinstance(exc, Exception) and (
                self.should_trip is None or self.should_trip(exc)
            )
            if not should_trip:
                async with self._state_lock:
                    self._half_open_probe_active = False
                    if self.state == CircuitState.HALF_OPEN and isinstance(exc, Exception):
                        self.failures = 0
                        self.state = CircuitState.CLOSED
                        self.opened_at = None
                raise
            async with self._state_lock:
                self._half_open_probe_active = False
                self.failures += 1
                if self.state == CircuitState.HALF_OPEN or self.failures >= self.failure_threshold:
                    self.state = CircuitState.OPEN
                    self.opened_at = datetime.now(UTC)
            raise
        async with self._state_lock:
            self._half_open_probe_active = False
            self.failures = 0
            self.state = CircuitState.CLOSED
            self.opened_at = None
        return result
