"""Retry utility for LLM provider calls with exponential backoff.

Provides a shared retry mechanism that classifies errors as retryable
(rate limits, server errors, transient connection issues) vs non-retryable
(auth failures, invalid requests, model not found) and applies exponential
backoff with jitter for retryable errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from cognis.logging import get_logger
from cognis.providers.retry import compute_delay

logger = get_logger(__name__)

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds
DEFAULT_JITTER = True


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry timing configuration for LLM operations."""

    stage: Literal["pre_stream", "mid_stream"]
    max_retries: int
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    jitter: bool = DEFAULT_JITTER


DEFAULT_PRE_STREAM_RETRY_POLICY = RetryPolicy("pre_stream", DEFAULT_MAX_RETRIES)
DEFAULT_MID_STREAM_RETRY_POLICY = RetryPolicy("mid_stream", 3, 1.0, 15.0)


def compute_retry_delay(policy: RetryPolicy, attempt: int) -> float:
    """Compute the delay for a retry attempt using the shared retry formula."""

    return compute_delay(attempt, policy.base_delay, policy.max_delay, policy.jitter)


_CONTEXT_OVERFLOW_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("context_length_exceeded", "context_length_exceeded"),
    ("model_context_window_exceeded", "model_context_window_exceeded"),
    ("exceeds the context window", "exceeds_context_window"),
    ("exceeded the context window", "exceeds_context_window"),
    ("context window exceeded", "context_window_exceeded"),
    ("maximum context length", "maximum_context_length"),
    ("max context length", "maximum_context_length"),
    ("prompt is too long", "prompt_too_long"),
    ("input is too long", "input_too_long"),
    ("tokens exceed", "tokens_exceed_limit"),
    ("token limit exceeded", "token_limit_exceeded"),
)

_CONTEXT_TERMS = ("context", "token", "tokens", "prompt", "input length", "input is")
_GENERIC_SIZE_TERMS = (
    "request too large",
    "request entity too large",
    "status code: 413",
    "http 413",
)


class LLMContextOverflowError(RuntimeError):
    """Raised when a provider rejects a request for exceeding context limits."""

    def __init__(
        self,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        reason: str = "context_overflow",
        original_message: str = "",
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.reason = reason
        self.original_message = original_message
        super().__init__(
            "LLM context overflow"
            f" provider={provider_id or 'unknown'!r}"
            f" model={model_id or 'unknown'!r}"
            f" reason={reason}"
        )


def context_overflow_reason(exc: BaseException | str) -> str | None:
    """Return a stable reason when an error indicates provider context overflow."""

    if isinstance(exc, LLMContextOverflowError):
        return exc.reason
    message = str(exc).lower()
    if not message:
        return None
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if body is not None:
        message = f"{message} {body!s}".lower()
        code = getattr(body, "code", None)
        if code:
            message = f"{message} {code!s}".lower()
        if isinstance(body, dict):
            body_code = body.get("code") or body.get("type")
            if body_code:
                message = f"{message} {body_code!s}".lower()
            nested_error = body.get("error")
            if nested_error is not None:
                message = f"{message} {nested_error!s}".lower()
                if isinstance(nested_error, dict):
                    nested_code = nested_error.get("code") or nested_error.get("type")
                    if nested_code:
                        message = f"{message} {nested_code!s}".lower()
    for signature, reason in _CONTEXT_OVERFLOW_SIGNATURES:
        if signature in message:
            return reason
    if "too many tokens" in message and any(
        marker in message for marker in ("context", "prompt", "input", "request", "maximum")
    ):
        return "too_many_tokens"
    if (status == 413 or any(term in message for term in _GENERIC_SIZE_TERMS)) and any(
        term in message for term in _CONTEXT_TERMS
    ):
        return "request_entity_too_large"
    return None


def is_context_overflow_error(exc: BaseException | str) -> bool:
    """Return True when *exc* is a provider context-window overflow."""

    return context_overflow_reason(exc) is not None


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

    if _looks_like_quota_exhaustion(exc):
        return False

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

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status in {429, 500, 502, 503, 504}:
        return True
    if status in {400, 401, 403, 404, 422}:
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


def _looks_like_quota_exhaustion(exc: Exception) -> bool:
    message = str(exc).lower()
    body = getattr(exc, "body", None)
    if body is not None:
        message = f"{message} {body!s}".lower()
        if isinstance(body, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else body
            code = error.get("code") or error.get("type")
            if code:
                message = f"{message} {code!s}".lower()
    return any(
        marker in message
        for marker in (
            "usage_limit_reached",
            "insufficient_quota",
            "quota exceeded",
            "quota_exceeded",
            "billing hard limit",
            "exceeded your current quota",
        )
    )


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
