"""Reusable circuit breaker helper for provider calls."""

from __future__ import annotations

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

    def _allow_call(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.opened_at is None:
                return False
            if datetime.now(UTC) - self.opened_at >= timedelta(seconds=self.recovery_timeout):
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    async def call(self, func: Callable[[], Awaitable[T]]) -> T:
        """Run a protected async function."""
        if not self._allow_call():
            raise CircuitBreakerError("Circuit breaker is open")
        try:
            result = await func()
        except Exception as exc:
            if self.should_trip is not None and not self.should_trip(exc):
                raise
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = datetime.now(UTC)
            raise
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at = None
        return result
