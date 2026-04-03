"""Retry utility for LLM provider calls with exponential backoff.

Provides a shared retry mechanism that classifies errors as retryable
(rate limits, server errors, transient connection issues) vs non-retryable
(auth failures, invalid requests, model not found) and applies exponential
backoff with jitter for retryable errors.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from cognis.logging import get_logger

logger = get_logger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds
DEFAULT_JITTER = True


def is_retryable_error(exc: Exception) -> bool:
    """Classify whether an LLM error is retryable.

    Retryable errors are transient issues that may resolve on retry:
    - Rate limits (429)
    - Server errors (500, 502, 503)
    - Connection errors
    - Timeouts
    - LiteLLM mid-stream failures

    Non-retryable errors should fail immediately:
    - Authentication failures (401, 403)
    - Invalid requests (400)
    - Model not found (404)
    - Content policy violations
    """
    exc_name = type(exc).__name__
    exc_msg = str(exc).lower()

    # LiteLLM-specific error types (check class name to avoid import dependency)
    non_retryable_names = {
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "BadRequestError",
        "UnprocessableEntityError",
        "ContentPolicyViolationError",
    }
    if exc_name in non_retryable_names:
        return False

    retryable_names = {
        "RateLimitError",
        "ServiceUnavailableError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "Timeout",
        "MidStreamFallbackError",
    }
    if exc_name in retryable_names:
        return True

    # Check HTTP status codes embedded in error messages
    for code in ("429", "500", "502", "503", "504"):
        if code in exc_msg:
            return True

    # Connection-related errors
    connection_keywords = (
        "connection",
        "timeout",
        "timed out",
        "rate limit",
        "rate_limit",
        "too many requests",
        "server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "temporarily unavailable",
    )
    if any(kw in exc_msg for kw in connection_keywords):
        return True

    # asyncio-level errors
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))


def _compute_delay(attempt: int, base_delay: float, max_delay: float, jitter: bool) -> float:
    """Compute delay with exponential backoff and optional jitter."""
    delay = min(base_delay * (2**attempt), max_delay)
    if jitter:
        # Add ±25% jitter to prevent thundering herd
        delay *= 0.75 + random.random() * 0.5  # noqa: S311
    return delay


async def with_llm_retry[T](
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: bool = DEFAULT_JITTER,
    operation: str = "LLM call",
    **kwargs: object,
) -> T:
    """Execute an async function with retry and exponential backoff.

    Only retries errors classified as retryable by ``is_retryable_error``.
    Non-retryable errors are raised immediately.

    Args:
        fn: Async function to call.
        *args: Positional arguments for fn.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay cap in seconds.
        jitter: Whether to add random jitter to delays.
        operation: Human-readable operation name for logging.
        **kwargs: Keyword arguments for fn.

    Returns:
        The return value of fn.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc

            if not is_retryable_error(exc):
                # Non-retryable — fail immediately
                raise

            if attempt >= max_retries:
                # Exhausted retries
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

            delay = _compute_delay(attempt, base_delay, max_delay, jitter)
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
