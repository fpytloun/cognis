from __future__ import annotations

import asyncio

import pytest

from cognis.api.chat_v2.background_event_reads import BackgroundEventReadAdmission


async def _assert_all_permits_available(
    admission: BackgroundEventReadAdmission,
    permit_count: int,
) -> None:
    started = 0
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        nonlocal started
        started += 1
        if started == permit_count:
            all_started.set()
        await release.wait()

    tasks = [asyncio.create_task(admission.run(operation)) for _ in range(permit_count)]
    await asyncio.wait_for(all_started.wait(), timeout=1)
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_failure_backs_off_one_probe_then_recovers() -> None:
    admission = BackgroundEventReadAdmission(
        initial_backoff_seconds=0.02,
        max_backoff_seconds=0.02,
        jitter_ratio=0,
    )
    calls = 0

    async def failing() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("Intaris unavailable")

    with pytest.raises(RuntimeError, match="Intaris"):
        await admission.run(failing)

    started = asyncio.get_running_loop().time()

    async def succeeding() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await admission.run(succeeding) == "ok"
    assert asyncio.get_running_loop().time() - started >= 0.018
    assert calls == 2
    assert admission.diagnostics()["failure_count"] == 0


@pytest.mark.asyncio
async def test_foreground_operation_is_not_coupled_to_background_backoff() -> None:
    admission = BackgroundEventReadAdmission(
        initial_backoff_seconds=1,
        max_backoff_seconds=1,
        jitter_ratio=0,
    )

    async def failing() -> None:
        raise RuntimeError("Intaris unavailable")

    with pytest.raises(RuntimeError):
        await admission.run(failing)

    foreground_calls = 0

    async def foreground() -> str:
        nonlocal foreground_calls
        foreground_calls += 1
        return "foreground"

    assert await foreground() == "foreground"
    assert foreground_calls == 1


@pytest.mark.asyncio
async def test_cancellation_during_success_release_preserves_every_permit() -> None:
    admission = BackgroundEventReadAdmission(max_concurrency=2)
    operation_started = asyncio.Event()
    finish_operation = asyncio.Event()

    async def operation() -> str:
        operation_started.set()
        await finish_operation.wait()
        return "success"

    task = asyncio.create_task(admission.run(operation))
    await operation_started.wait()
    await admission._state_lock.acquire()
    finish_operation.set()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    admission._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert admission.diagnostics() == {
        "failure_count": 0,
        "next_probe_in_seconds": 0.0,
        "probe_active": False,
    }
    await _assert_all_permits_available(admission, 2)


@pytest.mark.asyncio
async def test_cancellation_during_failure_release_preserves_backoff_and_permits() -> None:
    admission = BackgroundEventReadAdmission(
        max_concurrency=2,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
        jitter_ratio=0,
    )
    operation_started = asyncio.Event()
    fail_operation = asyncio.Event()

    async def initial_failure() -> None:
        raise RuntimeError("Intaris unavailable")

    with pytest.raises(RuntimeError, match="Intaris"):
        await admission.run(initial_failure)
    await asyncio.sleep(0.011)

    async def operation() -> None:
        operation_started.set()
        await fail_operation.wait()
        raise RuntimeError("Intaris unavailable")

    task = asyncio.create_task(admission.run(operation))
    await operation_started.wait()
    assert admission.diagnostics()["probe_active"] is True
    await admission._state_lock.acquire()
    fail_operation.set()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    admission._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    diagnostics = admission.diagnostics()
    assert diagnostics["failure_count"] == 2
    assert diagnostics["probe_active"] is False
    assert float(diagnostics["next_probe_in_seconds"]) > 0

    assert await admission.run(lambda: asyncio.sleep(0, result="recovered")) == "recovered"
    assert admission.diagnostics()["failure_count"] == 0
    await _assert_all_permits_available(admission, 2)


@pytest.mark.asyncio
async def test_cancelled_probe_operation_clears_probe_and_preserves_permits() -> None:
    admission = BackgroundEventReadAdmission(
        max_concurrency=2,
        initial_backoff_seconds=0.01,
        max_backoff_seconds=0.01,
        jitter_ratio=0,
    )

    async def initial_failure() -> None:
        raise RuntimeError("Intaris unavailable")

    with pytest.raises(RuntimeError, match="Intaris"):
        await admission.run(initial_failure)
    await asyncio.sleep(0.011)

    probe_started = asyncio.Event()
    hold_probe = asyncio.Event()

    async def probe() -> None:
        probe_started.set()
        await hold_probe.wait()

    task = asyncio.create_task(admission.run(probe))
    await probe_started.wait()
    assert admission.diagnostics()["probe_active"] is True
    await admission._state_lock.acquire()
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    admission._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    diagnostics = admission.diagnostics()
    assert diagnostics["failure_count"] == 1
    assert diagnostics["probe_active"] is False
    assert await admission.run(lambda: asyncio.sleep(0, result="recovered")) == "recovered"
    await _assert_all_permits_available(admission, 2)
