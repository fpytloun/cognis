"""Regression tests for connection-attempt backoff ownership."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import websockets

from cognis.executor import runner as runner_module
from cognis.models.tool import ExecutorConfig


def _mock_registration(
    runner: runner_module.ExecutorRunner,
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object] | list[object] | None = None,
) -> AsyncMock:
    ws = AsyncMock()

    async def receive() -> str:
        assert ws.send.await_args is not None
        ready = json.loads(ws.send.await_args.args[0])
        if isinstance(response, dict):
            return json.dumps({"id": ready["id"], **response})
        return json.dumps(
            response
            if response is not None
            else {
                "jsonrpc": "2.0",
                "id": ready["id"],
                "result": {"status": "registered", "executor_id": runner.config.executor_id},
            }
        )

    ws.recv.side_effect = receive
    connection = AsyncMock()
    connection.__aenter__.return_value = ws
    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(runner, "_refresh_resource_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(
        runner, "_build_ready_params", lambda _snapshot: {"executor_id": runner.config.executor_id}
    )
    return ws


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registrations", "expected_delays"),
    [
        ([False, False, False, True, False], [1, 2, 4, 1, 2]),
        ([True, True, True], [1, 1, 1]),
        ([False] * 8, [1, 2, 4, 8, 16, 32, 60, 60]),
    ],
)
async def test_backoff_resets_only_after_registration(
    monkeypatch: pytest.MonkeyPatch,
    registrations: list[bool],
    expected_delays: list[int],
) -> None:
    runner = runner_module.ExecutorRunner(
        ExecutorConfig(
            executor_id="executor-reconnect",
            controller_url="https://controller.test",
            controller_token="registration-token",
        )
    )
    _mock_registration(runner, monkeypatch)
    monkeypatch.setattr(
        runner, "_run_connection_loops", AsyncMock(side_effect=ConnectionError("Connection ended"))
    )
    monkeypatch.setattr(runner_module, "_EventLoopWatchdog", MagicMock())
    connect_and_serve = runner._connect_and_serve
    attempts = iter(registrations)
    delays: list[float] = []

    async def connect() -> None:
        if next(attempts):
            await connect_and_serve()
        else:
            raise ConnectionError("Connection attempt failed")

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == len(registrations):
            runner.stop()

    monkeypatch.setattr(runner, "_connect_and_serve", connect)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    await runner.run()

    assert delays == expected_delays


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"error": {"code": -32000, "message": "Registration rejected"}},
        [],
    ],
)
async def test_failed_registration_preserves_backoff(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object] | list[object],
) -> None:
    runner = runner_module.ExecutorRunner(
        ExecutorConfig(
            executor_id="executor-reconnect",
            controller_url="https://controller.test",
            controller_token="registration-token",
        )
    )
    runner._reconnect_delay = 60
    _mock_registration(runner, monkeypatch, response)
    loops = AsyncMock()
    monkeypatch.setattr(runner, "_run_connection_loops", loops)

    if isinstance(response, list):
        with pytest.raises(ValueError, match="Invalid executor registration response"):
            await runner._connect_and_serve()
    else:
        await runner._connect_and_serve()

    assert runner._reconnect_delay == 60
    loops.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"result": {"status": "initializing", "executor_id": "executor-reconnect"}},
        {"result": {"status": "registered", "executor_id": "different-executor"}},
        {
            "id": "different-registration",
            "result": {"status": "registered", "executor_id": "executor-reconnect"},
        },
    ],
)
async def test_unmatched_registration_does_not_clear_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    runner = runner_module.ExecutorRunner(
        ExecutorConfig(
            executor_id="executor-reconnect",
            controller_url="https://controller.test",
            controller_token="registration-token",
        )
    )
    runner._reconnect_delay = 60
    _mock_registration(runner, monkeypatch, response)
    monkeypatch.setattr(runner, "_run_connection_loops", AsyncMock())

    await runner._connect_and_serve()

    assert runner._reconnect_delay == 60


@pytest.mark.asyncio
async def test_cancellation_does_not_start_another_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = runner_module.ExecutorRunner(ExecutorConfig(executor_id="executor-reconnect"))
    connect = AsyncMock(side_effect=asyncio.CancelledError)
    sleep = AsyncMock()
    monkeypatch.setattr(runner, "_connect_and_serve", connect)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(runner_module, "_EventLoopWatchdog", MagicMock())

    with pytest.raises(asyncio.CancelledError):
        await runner.run()

    connect.assert_awaited_once()
    sleep.assert_not_awaited()
