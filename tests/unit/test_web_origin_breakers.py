from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreakerError, CircuitState
from cognis.tools.executor.web.backends import direct


@pytest.mark.asyncio
async def test_client_errors_do_not_trip_origin_breaker() -> None:
    direct._fetch_breakers.clear()
    request = httpx.Request("GET", "https://dead.example/missing")
    response = httpx.Response(404, request=request)
    error = httpx.HTTPStatusError("missing", request=request, response=response)
    with patch(
        "cognis.tools.executor.web.backends.direct.fetch_with_retry",
        new=AsyncMock(side_effect=error),
    ):
        for _ in range(8):
            result = await direct._origin_breaker(str(request.url)).call(
                lambda: direct._fetch_without_counting_client_errors(
                    str(request.url),
                    timeout=5,
                )
            )
            assert isinstance(result, ToolResult)
            assert result.metadata["http_status"] == 404
    breaker = direct._origin_breaker(str(request.url))
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failures == 0


@pytest.mark.asyncio
async def test_origin_breaker_isolates_unrelated_hosts() -> None:
    direct._fetch_breakers.clear()
    broken = direct._origin_breaker("https://broken.example/a")
    healthy = direct._origin_breaker("https://healthy.example/a")

    async def fail() -> None:
        raise httpx.ConnectError("offline")

    for _ in range(5):
        with pytest.raises(httpx.ConnectError):
            await broken.call(fail)
    assert broken.state == CircuitState.OPEN
    assert healthy.state == CircuitState.CLOSED
    assert await healthy.call(lambda: _return_value("ok")) == "ok"


async def _return_value(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_only_one_half_open_probe_is_admitted() -> None:
    breaker = direct.CircuitBreaker(failure_threshold=1, recovery_timeout=0)

    async def fail() -> None:
        raise httpx.ConnectError("offline")

    with pytest.raises(httpx.ConnectError):
        await breaker.call(fail)
    admitted = await breaker._allow_call()
    assert admitted is True
    assert await breaker._allow_call() is False
    with pytest.raises(CircuitBreakerError):
        await breaker.call(lambda: _return_value("blocked"))


@pytest.mark.asyncio
async def test_cancelled_half_open_probe_releases_admission() -> None:
    breaker = direct.CircuitBreaker(failure_threshold=1, recovery_timeout=0)

    async def fail() -> None:
        raise httpx.ConnectError("offline")

    async def cancel() -> None:
        raise __import__("asyncio").CancelledError

    with pytest.raises(httpx.ConnectError):
        await breaker.call(fail)
    with pytest.raises(__import__("asyncio").CancelledError):
        await breaker.call(cancel)
    assert breaker._half_open_probe_active is False
    assert await breaker.call(lambda: _return_value("recovered")) == "recovered"


@pytest.mark.asyncio
async def test_non_trip_half_open_exception_closes_breaker() -> None:
    breaker = direct.CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=0,
        should_trip=lambda exc: not isinstance(exc, ValueError),
    )

    async def fail() -> None:
        raise httpx.ConnectError("offline")

    async def client_error() -> None:
        raise ValueError("not transient")

    with pytest.raises(httpx.ConnectError):
        await breaker.call(fail)
    with pytest.raises(ValueError):
        await breaker.call(client_error)
    assert breaker.state == CircuitState.CLOSED
    assert breaker._half_open_probe_active is False
