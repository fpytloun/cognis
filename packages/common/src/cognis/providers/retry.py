"""Provider-agnostic retry primitives shared by controller and executor."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from cognis.logging import get_logger
from cognis.providers.circuit_breaker import CircuitBreakerError

logger = get_logger(__name__)


def compute_delay(attempt: int, base_delay: float, max_delay: float, jitter: bool) -> float:
    """Compute bounded exponential backoff with optional jitter."""
    delay = min(base_delay * (2**attempt), max_delay)
    if jitter:
        delay *= 0.75 + float(random.random()) * 0.5  # noqa: S311
    return float(delay)


def is_retryable_http_error(exc: Exception) -> bool:
    """Classify transient errors without depending on a concrete HTTP client."""
    if isinstance(exc, CircuitBreakerError):
        return False
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)) or type(
        exc
    ).__name__ in {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
    }


async def with_retry[T](
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retryable_check: Callable[[Exception], bool] | None = None,
    operation: str = "provider call",
    **kwargs: object,
) -> T:
    """Run an async provider operation with bounded retry and backoff."""
    check = retryable_check or is_retryable_http_error
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            if not check(exc):
                raise
            if attempt >= max_retries:
                raise
            delay = compute_delay(attempt, base_delay, max_delay, jitter)
            logger.warning(
                "%s failed (attempt %d/%d), retrying in %.1fs",
                operation,
                attempt + 1,
                max_retries + 1,
                delay,
                extra={"extra_data": {"error_type": type(exc).__name__, "delay": delay}},
            )
            await asyncio.sleep(delay)
    raise AssertionError("retry loop did not return or raise")
