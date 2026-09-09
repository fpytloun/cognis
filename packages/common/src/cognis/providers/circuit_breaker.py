"""Reusable circuit breaker helper for provider calls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(RuntimeError):
    """Raised when the circuit breaker is open."""

    def __init__(
        self,
        message: str | None = None,
        *,
        name: str | None = None,
        state: CircuitState = CircuitState.OPEN,
        retry_after_seconds: float = 0.0,
        operation: str | None = None,
        last_error_type: str | None = None,
        last_failure_at: datetime | None = None,
    ) -> None:
        label = f" '{name}'" if name else ""
        super().__init__(message if message is not None else f"Circuit breaker{label} is open")
        self.name = name
        self.state = state
        self.retry_after_seconds = retry_after_seconds
        self.operation = operation
        self.last_error_type = last_error_type
        self.last_failure_at = last_failure_at

    def metadata(self) -> dict[str, Any]:
        """Return non-secret diagnostics suitable for logs and tool metadata."""

        return {
            "name": self.name,
            "state": self.state.value,
            "retry_after_seconds": self.retry_after_seconds,
            "operation": self.operation,
            "last_error_type": self.last_error_type,
            "last_failure_at": (
                self.last_failure_at.isoformat() if self.last_failure_at is not None else None
            ),
        }


class CircuitBreaker:
    """Simple async circuit breaker."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        *,
        name: str | None = None,
        should_trip: Callable[[Exception], bool] | None = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.name = name
        self.should_trip = should_trip
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at: datetime | None = None
        self.last_failure_at: datetime | None = None
        self.last_error_type: str | None = None
        self._state_lock = asyncio.Lock()
        self._half_open_probe_active = False

    def _retry_after_seconds(self, *, now: datetime | None = None) -> float:
        if self.opened_at is None:
            return self.recovery_timeout
        elapsed = ((now or datetime.now(UTC)) - self.opened_at).total_seconds()
        return max(0.0, self.recovery_timeout - elapsed)

    def _log_transition(
        self,
        previous: CircuitState,
        current: CircuitState,
        *,
        operation: str | None,
        error_type: str | None = None,
        reason: str | None = None,
    ) -> None:
        if self.name is None or previous == current:
            return
        log = logger.warning if current == CircuitState.OPEN else logger.info
        log(
            "circuit breaker state changed",
            extra={
                "extra_data": {
                    "circuit_name": self.name,
                    "previous_state": previous.value,
                    "state": current.value,
                    "failure_count": self.failures,
                    "failure_threshold": self.failure_threshold,
                    "recovery_timeout_seconds": self.recovery_timeout,
                    "operation": operation,
                    "error_type": error_type,
                    "reason": reason,
                }
            },
        )

    async def _allow_call(self, *, operation: str | None = None) -> bool:
        async with self._state_lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if self.opened_at is None:
                    return False
                if datetime.now(UTC) - self.opened_at < timedelta(seconds=self.recovery_timeout):
                    return False
                previous = self.state
                self.state = CircuitState.HALF_OPEN
                self._log_transition(previous, self.state, operation=operation)
            if self._half_open_probe_active:
                return False
            self._half_open_probe_active = True
            return True

    async def reset(
        self,
        *,
        reason: str,
        operation: str | None = None,
    ) -> None:
        """Close the breaker after independent evidence that the dependency recovered."""

        if self.state == CircuitState.CLOSED and self.failures == 0:
            return
        async with self._state_lock:
            self.reset_nowait(reason=reason, operation=operation)

    def reset_nowait(
        self,
        *,
        reason: str,
        operation: str | None = None,
    ) -> None:
        """Close the breaker in one non-blocking event-loop effect."""

        previous = self.state
        self._half_open_probe_active = False
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at = None
        self.last_failure_at = None
        self.last_error_type = None
        self._log_transition(
            previous,
            self.state,
            operation=operation,
            reason=reason,
        )

    async def call(
        self,
        func: Callable[[], Awaitable[T]],
        *,
        operation: str | None = None,
    ) -> T:
        """Run a protected async function."""
        if not await self._allow_call(operation=operation):
            raise CircuitBreakerError(
                name=self.name,
                state=self.state,
                retry_after_seconds=self._retry_after_seconds(),
                operation=operation,
                last_error_type=self.last_error_type,
                last_failure_at=self.last_failure_at,
            )
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
                        non_trip_previous = self.state
                        self.failures = 0
                        self.state = CircuitState.CLOSED
                        self.opened_at = None
                        self.last_failure_at = None
                        self.last_error_type = None
                        self._log_transition(
                            non_trip_previous,
                            self.state,
                            operation=operation,
                            error_type=type(exc).__name__,
                            reason="non_trip_exception",
                        )
                raise
            async with self._state_lock:
                self._half_open_probe_active = False
                self.failures += 1
                now = datetime.now(UTC)
                self.last_failure_at = now
                self.last_error_type = type(exc).__name__
                if self.state == CircuitState.HALF_OPEN or self.failures >= self.failure_threshold:
                    failure_previous = self.state
                    self.state = CircuitState.OPEN
                    self.opened_at = now
                    self._log_transition(
                        failure_previous,
                        self.state,
                        operation=operation,
                        error_type=self.last_error_type,
                        reason="failure_threshold",
                    )
            raise
        async with self._state_lock:
            success_previous = self.state
            self._half_open_probe_active = False
            self.failures = 0
            self.state = CircuitState.CLOSED
            self.opened_at = None
            self.last_failure_at = None
            self.last_error_type = None
            self._log_transition(
                success_previous,
                self.state,
                operation=operation,
                reason="protected_call_succeeded",
            )
        return result
