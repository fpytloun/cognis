from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError, CircuitState


def test_circuit_breaker_error_preserves_legacy_positional_constructor() -> None:
    error = CircuitBreakerError("legacy circuit message")
    empty_error = CircuitBreakerError("")

    assert str(error) == "legacy circuit message"
    assert str(empty_error) == ""
    assert error.metadata() == {
        "name": None,
        "state": "open",
        "retry_after_seconds": 0.0,
        "operation": None,
        "last_error_type": None,
        "last_failure_at": None,
    }


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
async def test_circuit_breaker_open_error_contains_safe_diagnostics() -> None:
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=30,
        name="executor.websocket:exec-1",
    )
    with pytest.raises(RuntimeError):
        await breaker.call(_raise_runtime_error, operation="tool.execute")

    with pytest.raises(CircuitBreakerError) as exc:
        await breaker.call(
            lambda: _return_value("blocked"),
            operation="tool.execute",
        )

    metadata = exc.value.metadata()
    assert metadata["name"] == "executor.websocket:exec-1"
    assert metadata["state"] == "open"
    assert metadata["retry_after_seconds"] == pytest.approx(30, abs=1)
    assert metadata["operation"] == "tool.execute"
    assert metadata["last_error_type"] == "RuntimeError"
    assert metadata["last_failure_at"] is not None


@pytest.mark.asyncio
async def test_circuit_breaker_logs_named_transitions_without_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=30,
        name="executor.websocket:exec-1",
    )
    caplog.set_level(logging.INFO, logger="cognis.providers.circuit_breaker")

    with pytest.raises(RuntimeError):
        await breaker.call(_raise_sensitive_runtime_error, operation="tool.execute")
    await breaker.reset(reason="owned_inbound_frame", operation="receive")

    transitions = [
        record.extra_data
        for record in caplog.records
        if record.message == "circuit breaker state changed"
    ]
    assert [transition["state"] for transition in transitions] == ["open", "closed"]
    assert transitions[0]["error_type"] == "RuntimeError"
    assert transitions[1]["reason"] == "owned_inbound_frame"
    assert "sensitive-command-argument" not in caplog.text


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


@pytest.mark.asyncio
async def test_circuit_breaker_ignores_non_trip_exceptions() -> None:
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_timeout=30,
        should_trip=lambda exc: not isinstance(exc, ValueError),
    )

    with pytest.raises(ValueError):
        await breaker.call(_raise_value_error)

    assert breaker.failures == 0
    assert breaker.state == CircuitState.CLOSED


async def _return_value(value: str) -> str:
    return value


async def _raise_runtime_error() -> str:
    raise RuntimeError("boom")


async def _raise_value_error() -> str:
    raise ValueError("boom")


async def _raise_sensitive_runtime_error() -> str:
    raise RuntimeError("sensitive-command-argument")
