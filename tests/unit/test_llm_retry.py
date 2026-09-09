from __future__ import annotations

from types import ModuleType

import httpx
import pytest

from cognis.executor.providers.llm import retry as executor_llm_retry
from cognis.providers.llm import retry as llm_retry
from cognis.providers.llm.anthropic.transport import AnthropicTransportError


def _status_error(status: int, *, retry_after: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat")
    response = httpx.Response(status, headers={"Retry-After": retry_after}, request=request)
    return httpx.HTTPStatusError("provider failed", request=request, response=response)


@pytest.mark.asyncio
async def test_with_llm_retry_uses_bounded_provider_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _status_error(429, retry_after="3.5")
        return "ok"

    result = await llm_retry.with_llm_retry(flaky, max_retries=1, max_delay=10, jitter=False)

    assert result == "ok"
    assert calls == 2
    assert sleeps == [3.5]


@pytest.mark.asyncio
async def test_with_llm_retry_does_not_retry_past_inline_retry_after_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(llm_retry.asyncio, "sleep", fake_sleep)
    calls = 0

    async def rate_limited() -> str:
        nonlocal calls
        calls += 1
        raise _status_error(429, retry_after="300")

    with pytest.raises(httpx.HTTPStatusError):
        await llm_retry.with_llm_retry(rate_limited, max_retries=3, max_delay=10, jitter=False)

    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("retry_module", "error_type"),
    [
        (retry_module, error_type)
        for retry_module in (llm_retry, executor_llm_retry)
        for error_type in (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.ReadError,
        )
    ],
)
def test_httpx_transient_errors_are_retryable_without_message(
    retry_module: ModuleType,
    error_type: type[httpx.HTTPError],
) -> None:
    request = httpx.Request("POST", "https://provider.example/v1/chat")

    assert retry_module.is_retryable_error(error_type("", request=request))


@pytest.mark.parametrize("retry_module", [llm_retry, executor_llm_retry])
def test_wrapped_native_transport_connection_error_is_retryable(
    retry_module: ModuleType,
) -> None:
    error = AnthropicTransportError(
        "Anthropic Messages transport request failed",
        payload={"category": "connection", "message": "ReadTimeout"},
    )

    assert retry_module.is_retryable_error(error)


@pytest.mark.parametrize("retry_module", [llm_retry, executor_llm_retry])
@pytest.mark.asyncio
async def test_with_llm_retry_uses_normalized_anthropic_retry_after(
    monkeypatch: pytest.MonkeyPatch,
    retry_module: ModuleType,
) -> None:
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AnthropicTransportError(
                "rate limited",
                status_code=429,
                payload={
                    "category": "rate_limit",
                    "message": "rate limited",
                    "retry_after_seconds": 23.0,
                },
            )
        return "ok"

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)

    assert await retry_module.with_llm_retry(flaky, max_retries=1, max_delay=30) == "ok"
    assert sleeps == [23.0]


@pytest.mark.parametrize("retry_module", [llm_retry, executor_llm_retry])
@pytest.mark.asyncio
async def test_with_llm_retry_rejects_normalized_retry_after_beyond_cap(
    monkeypatch: pytest.MonkeyPatch,
    retry_module: ModuleType,
) -> None:
    sleeps: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def rate_limited() -> str:
        nonlocal calls
        calls += 1
        raise AnthropicTransportError(
            "rate limited",
            status_code=429,
            payload={
                "category": "rate_limit",
                "message": "rate limited",
                "retry_after_seconds": 60.0,
            },
        )

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(AnthropicTransportError):
        await retry_module.with_llm_retry(rate_limited, max_retries=3, max_delay=30)
    assert calls == 1
    assert sleeps == []
