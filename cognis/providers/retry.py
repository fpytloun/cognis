"""Shared retry utility for HTTP provider calls with exponential backoff.

Provides a generic retry mechanism for httpx-based provider calls that
classifies errors as retryable (rate limits, server errors, transient
connection issues) vs non-retryable (auth failures, invalid requests)
and applies exponential backoff with jitter.

This module is provider-agnostic.  Provider-specific retry utilities
(e.g. ``providers/llm/retry.py``) may import ``compute_delay`` from
here and add their own error classification.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import httpx

from cognis.logging import get_logger
from cognis.providers.circuit_breaker import CircuitBreakerError

logger = get_logger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds
DEFAULT_JITTER = True


def compute_delay(attempt: int, base_delay: float, max_delay: float, jitter: bool) -> float:
    """Compute delay with exponential backoff and optional jitter.

    Delay formula: ``min(base_delay * 2^attempt, max_delay)`` with ±25%
    jitter to prevent thundering herd.
    """
    delay = min(base_delay * (2**attempt), max_delay)
    if jitter:
        delay *= 0.75 + float(random.random()) * 0.5  # noqa: S311
    return delay


def is_retryable_http_error(exc: Exception) -> bool:
    """Classify whether an HTTP error is retryable.

    Retryable errors are transient issues that may resolve on retry:
    - Rate limits (HTTP 429)
    - Server errors (HTTP 500, 502, 503, 504)
    - Connection errors (httpx.ConnectError, httpx.ConnectTimeout)
    - Timeouts (httpx.TimeoutException, asyncio.TimeoutError)

    Non-retryable errors should fail immediately:
    - Circuit breaker open (CircuitBreakerError)
    - Authentication failures (HTTP 401, 403)
    - Invalid requests (HTTP 400)
    - Not found (HTTP 404)
    - Conflict (HTTP 409)
    - Unprocessable entity (HTTP 422)
    """
    # Circuit breaker open — never retry, the service is known-down
    if isinstance(exc, CircuitBreakerError):
        return False

    # httpx-specific error types
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.ConnectError):
        return True

    # HTTP status code errors
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}

    # asyncio-level errors; unknown errors are not retried
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))


async def with_retry[T](
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: bool = DEFAULT_JITTER,
    retryable_check: Callable[[Exception], bool] | None = None,
    operation: str = "HTTP call",
    **kwargs: object,
) -> T:
    """Execute an async function with retry and exponential backoff.

    Only retries errors classified as retryable by *retryable_check*
    (defaults to :func:`is_retryable_http_error`).  Non-retryable errors
    are raised immediately.

    Args:
        fn: Async function to call.
        *args: Positional arguments for *fn*.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay cap in seconds.
        jitter: Whether to add random jitter to delays.
        retryable_check: Callable that returns ``True`` for retryable
            errors.  Defaults to :func:`is_retryable_http_error`.
        operation: Human-readable operation name for logging.
        **kwargs: Keyword arguments for *fn*.

    Returns:
        The return value of *fn*.

    Raises:
        The last exception if all retries are exhausted.
    """
    check = retryable_check or is_retryable_http_error
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc

            if not check(exc):
                raise

            if attempt >= max_retries:
                logger.error(
                    "%s failed after %d retries",
                    operation,
                    max_retries,
                    extra={
                        "extra_data": {
                            "error_type": type(exc).__name__,
                            "attempts": attempt + 1,
                        }
                    },
                )
                raise

            delay = compute_delay(attempt, base_delay, max_delay, jitter)
            logger.warning(
                "%s failed (attempt %d/%d), retrying in %.1fs",
                operation,
                attempt + 1,
                max_retries + 1,
                delay,
                extra={
                    "extra_data": {
                        "error_type": type(exc).__name__,
                        "delay": delay,
                    }
                },
            )
            await asyncio.sleep(delay)

    # Should not reach here, but satisfy type checker
    assert last_exc is not None  # noqa: S101
    raise last_exc
