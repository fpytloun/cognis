from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from cognis.providers.llm.anthropic_subscription import AnthropicSubscriptionError
from cognis.providers.llm.errors import (
    MidStreamErrorCategory,
    classify_llm_exception,
    retry_after_seconds_from_headers,
)


def test_classify_anthropic_subscription_rate_limit_preserves_retry_after_header() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        429,
        headers={"Retry-After": "23"},
        json={"error": {"type": "rate_limit_error", "message": "slow down"}},
        request=request,
    )
    exc = AnthropicSubscriptionError(
        429,
        response.text,
        response=response,
        body=response.json(),
    )

    payload = classify_llm_exception(exc)

    assert payload["category"] == MidStreamErrorCategory.RATE_LIMIT.value
    assert payload["code"] == "rate_limit_error"
    assert payload["retry_after_seconds"] == 23


def test_retry_after_parser_accepts_http_date() -> None:
    retry_at = datetime.now(UTC) + timedelta(seconds=90)
    seconds = retry_after_seconds_from_headers(
        {"Retry-After": format_datetime(retry_at, usegmt=True)}
    )

    assert seconds is not None
    assert 0 < seconds <= 90


def test_retry_after_parser_rejects_non_finite_values() -> None:
    assert retry_after_seconds_from_headers({"Retry-After": "Infinity"}) is None
    assert retry_after_seconds_from_headers({"Retry-After": "NaN"}) is None
