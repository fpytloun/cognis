from __future__ import annotations

import httpx
import pytest

from cognis.providers.llm import retry as llm_retry


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
