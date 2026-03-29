from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_success_resets_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30)
    breaker.failures = 1

    result = await breaker.call(lambda: _return_value("ok"))

    assert result == "ok"
    assert breaker.failures == 0
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30)

    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error)
    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error)

    assert breaker.state == CircuitState.OPEN
    assert breaker.opened_at is not None


@pytest.mark.asyncio
async def test_circuit_breaker_rejects_calls_while_open() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30)
    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error)

    with pytest.raises(CircuitBreakerError):
        await breaker.call(lambda: _return_value("blocked"))


@pytest.mark.asyncio
async def test_circuit_breaker_moves_to_half_open_then_closes_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30)
    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error)

    breaker.opened_at = datetime.now(UTC) - timedelta(seconds=31)
    result = await breaker.call(lambda: _return_value("recovered"))

    assert result == "recovered"
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_reopens_when_half_open_call_fails() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30)
    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error)

    breaker.opened_at = datetime.now(UTC) - timedelta(seconds=31)
    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error)

    assert breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_allows_concurrent_successful_calls() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=30)

    first = breaker.call(lambda: _return_value("first"))
    second = breaker.call(lambda: _return_value("second"))

    assert await first == "first"
    assert await second == "second"
    assert breaker.state == CircuitState.CLOSED


async def _return_value(value: str) -> str:
    return value


async def _raise_runtime_error() -> str:
    raise RuntimeError("boom")
