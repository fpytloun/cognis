from __future__ import annotations

import asyncio
import signal
from time import monotonic
from unittest.mock import AsyncMock, Mock

import pytest
import uvicorn

from cognis.api.server import DrainingServer
from cognis.core.shutdown import ShutdownCallbacks, ShutdownCoordinator


@pytest.mark.asyncio
async def test_shutdown_drain_is_ordered_and_idempotent() -> None:
    order: list[str] = []
    coordinator = ShutdownCoordinator()

    async def begin() -> None:
        order.append("begin")

    async def turns(drain_timeout: float, cancel_timeout: float) -> dict[str, int]:
        assert drain_timeout > 0
        assert cancel_timeout > 0
        order.append("turns")
        return {"timed_out": 0}

    async def tools(timeout: float) -> bool:
        assert timeout > 0
        order.append("tools")
        return True

    coordinator.configure(
        ShutdownCallbacks(begin, turns, tools),
        drain_timeout_seconds=1,
        cancel_timeout_seconds=1,
    )

    await asyncio.gather(coordinator.drain(), coordinator.drain())
    await coordinator.drain()

    assert order == ["begin", "turns", "tools"]


@pytest.mark.asyncio
async def test_forced_shutdown_stops_wait_without_cancelling_work() -> None:
    drain_wait_cancelled = False
    started = asyncio.Event()
    coordinator = ShutdownCoordinator()

    async def begin() -> None:
        return None

    async def turns(_drain_timeout: float, _cancel_timeout: float) -> dict[str, int]:
        nonlocal drain_wait_cancelled
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            drain_wait_cancelled = True
            raise

    async def tools(_timeout: float) -> bool:
        raise AssertionError("tool drain must not start after force")

    coordinator.configure(
        ShutdownCallbacks(begin, turns, tools),
        drain_timeout_seconds=30,
        cancel_timeout_seconds=10,
    )
    draining = asyncio.create_task(coordinator.drain())
    await started.wait()

    coordinator.force()
    await asyncio.wait_for(draining, timeout=1)

    assert drain_wait_cancelled is True


@pytest.mark.asyncio
async def test_zero_settlement_budget_still_closes_admission() -> None:
    begin_calls = 0
    coordinator = ShutdownCoordinator()

    async def begin() -> None:
        nonlocal begin_calls
        begin_calls += 1

    async def turns(_drain_timeout: float, _cancel_timeout: float) -> dict[str, int]:
        raise AssertionError("turn drain must not start without settlement budget")

    async def tools(_timeout: float) -> bool:
        raise AssertionError("tool drain must not start without settlement budget")

    coordinator.configure(
        ShutdownCallbacks(begin, turns, tools),
        drain_timeout_seconds=0,
        cancel_timeout_seconds=0,
    )

    await coordinator.drain()

    assert begin_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_stage", "expected_order"),
    [
        ("begin", ["begin", "turns", "tools"]),
        ("turns", ["begin", "turns", "tools"]),
        ("tools", ["begin", "turns", "tools"]),
    ],
)
async def test_shutdown_continues_after_stage_failure(
    failed_stage: str,
    expected_order: list[str],
) -> None:
    order: list[str] = []
    coordinator = ShutdownCoordinator()

    async def begin() -> None:
        order.append("begin")
        if failed_stage == "begin":
            raise RuntimeError("begin failed")

    async def turns(_drain_timeout: float, _cancel_timeout: float) -> dict[str, int]:
        order.append("turns")
        if failed_stage == "turns":
            raise RuntimeError("turns failed")
        return {"timed_out": 0}

    async def tools(_timeout: float) -> bool:
        order.append("tools")
        if failed_stage == "tools":
            raise RuntimeError("tools failed")
        return True

    coordinator.configure(
        ShutdownCallbacks(begin, turns, tools),
        drain_timeout_seconds=1,
        cancel_timeout_seconds=1,
    )

    await coordinator.drain()

    assert order == expected_order


@pytest.mark.asyncio
async def test_cancellation_suppressing_stage_does_not_block_shutdown() -> None:
    release = asyncio.Event()
    retained_finished = asyncio.Event()
    coordinator = ShutdownCoordinator()
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def begin() -> None:
        return None

    async def turns(_drain_timeout: float, _cancel_timeout: float) -> dict[str, int]:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            await release.wait()
            retained_finished.set()
            raise RuntimeError("late retained failure") from None

    async def tools(_timeout: float) -> bool:
        return True

    coordinator.configure(
        ShutdownCallbacks(begin, turns, tools),
        drain_timeout_seconds=0.01,
        cancel_timeout_seconds=0,
    )
    started = monotonic()

    try:
        await coordinator.drain()

        assert monotonic() - started < 0.5
        release.set()
        await asyncio.wait_for(retained_finished.wait(), timeout=1)
        await asyncio.sleep(0)
        assert coordinator._retained_tasks == set()
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_exception_handler)


def test_repeated_termination_signal_forces_server_exit() -> None:
    coordinator = Mock(spec=ShutdownCoordinator)
    server = DrainingServer(
        uvicorn.Config(lambda _scope, _receive, _send: None, log_config=None),
        shutdown_coordinator=coordinator,
    )

    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit is True
    assert server.force_exit is False
    coordinator.force.assert_not_called()

    server.handle_exit(signal.SIGTERM, None)
    assert server.force_exit is True
    coordinator.force.assert_called_once_with()


@pytest.mark.asyncio
async def test_server_closes_transports_when_drain_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = Mock(spec=ShutdownCoordinator)
    coordinator.drain = AsyncMock(side_effect=RuntimeError("drain failed"))
    uvicorn_shutdown = AsyncMock()
    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    server = DrainingServer(
        uvicorn.Config(lambda _scope, _receive, _send: None, log_config=None),
        shutdown_coordinator=coordinator,
    )

    with pytest.raises(RuntimeError, match="drain failed"):
        await server.shutdown()

    uvicorn_shutdown.assert_awaited_once_with(sockets=None)
