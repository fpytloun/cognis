"""Intaris failures keep their dependency identity without replaying denial."""

from types import SimpleNamespace

import httpx
import pytest

from cognis.core.turn_scheduler import classify_turn_error
from cognis.providers.guardrails.errors import IntarisHTTPError, IntarisTransportError
from cognis.providers.guardrails.intaris import IntarisProvider


@pytest.mark.asyncio
async def test_auth_denial_is_not_retried_or_classified_as_llm() -> None:
    provider = IntarisProvider("https://guardrails.example.com", auth_provider=SimpleNamespace())
    request = httpx.Request("GET", "https://guardrails.example.com/api/v1/session/private/events")
    response = httpx.Response(401, request=request, text="private upstream content")
    attempts = 0

    async def denied() -> None:
        nonlocal attempts
        attempts += 1
        response.raise_for_status()

    with pytest.raises(IntarisHTTPError) as caught:
        await provider._call_with_retry(denied, operation="read_events")
    assert attempts == 1
    assert caught.value.__cause__ is not None
    error = await classify_turn_error(SimpleNamespace(), caught.value)
    assert error.code == "provider_error:guardrails"
    assert error.transient is False
    assert error.recoverable is False
    assert error.detail["operation"] == "read_events"
    assert error.detail["status_code"] == 401
    assert error.detail["provider"] == "intaris"
    assert "private" not in error.message
    assert "private" not in str(caught.value)
    await provider.client.aclose()


@pytest.mark.asyncio
async def test_transport_failure_keeps_transient_dependency_identity() -> None:
    original = httpx.ReadTimeout(
        "private transport diagnostic",
        request=httpx.Request("GET", "https://guardrails.example.com"),
    )
    error = await classify_turn_error(
        SimpleNamespace(), IntarisTransportError(original, operation="read_events")
    )
    assert error.code == "provider_error:guardrails"
    assert error.detail["provider"] == "intaris"
    assert error.transient and error.recoverable
    assert "private" not in error.message
