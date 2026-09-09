from __future__ import annotations

import asyncio
from time import monotonic
from unittest.mock import AsyncMock

import pytest

import cognis.core.direct_turn_runtime as direct_turn_runtime_module
from cognis.core.direct_turn_runtime import DurableDirectTurnRuntime
from cognis.core.remember_queue import RememberQueueItem, RememberRetryQueue
from cognis.core.tool_classification_queue import ToolClassificationQueue


@pytest.mark.asyncio
async def test_idle_remember_queue_waits_for_wake() -> None:
    queue = RememberRetryQueue(object(), recovery_interval_seconds=60)
    collect = AsyncMock(return_value=[])
    queue._collect_ready_in_memory = collect  # type: ignore[method-assign]

    await queue.start()
    await asyncio.sleep(0.05)
    assert collect.await_count == 1
    queue._signal_wake()  # noqa: SLF001
    async with asyncio.timeout(1):
        while collect.await_count < 2:
            await asyncio.sleep(0)
    await queue.stop()


@pytest.mark.asyncio
async def test_remember_queue_stop_drains_in_memory_backoff() -> None:
    worker = AsyncMock()
    queue = RememberRetryQueue(worker, recovery_interval_seconds=60)
    queue._items.append(  # noqa: SLF001
        RememberQueueItem(
            payload={"session_id": "session-1", "messages": []},
            next_retry_at=monotonic() + 60,
        )
    )

    await queue.start()
    await asyncio.sleep(0.05)
    worker.remember.assert_not_awaited()
    await queue.stop()

    worker.remember.assert_awaited_once_with(session_id="session-1", messages=[])


@pytest.mark.asyncio
async def test_idle_classification_queue_waits_for_wake() -> None:
    queue = ToolClassificationQueue(
        session_factory=lambda: None,
        llm_provider=object(),
        poll_interval_seconds=60,
    )
    claim = AsyncMock(return_value=[])
    queue._claim_due_items = claim  # type: ignore[method-assign]

    await queue.start()
    await asyncio.sleep(0.05)
    assert claim.await_count == 1
    queue._signal_wake()  # noqa: SLF001
    async with asyncio.timeout(1):
        while claim.await_count < 2:
            await asyncio.sleep(0)
    await queue.stop()


@pytest.mark.asyncio
async def test_idle_direct_turn_worker_waits_for_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = object.__new__(DurableDirectTurnRuntime)
    runtime._stop = asyncio.Event()
    runtime._wake = asyncio.Event()
    runtime._wake_generation = 0
    runtime._accepting_claims = True
    runtime.run_once = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        direct_turn_runtime_module,
        "DIRECT_TURN_RECOVERY_INTERVAL_SECONDS",
        60,
    )

    worker = asyncio.create_task(runtime._run())  # noqa: SLF001
    await asyncio.sleep(0.05)
    assert runtime.run_once.await_count == 1
    await runtime.wake()
    async with asyncio.timeout(1):
        while runtime.run_once.await_count < 2:
            await asyncio.sleep(0)
    runtime._stop.set()
    await runtime.wake()
    await worker
