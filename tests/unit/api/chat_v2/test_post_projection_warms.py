from __future__ import annotations

import asyncio

import pytest

from cognis.api.chat_v2.post_projection_warms import PostProjectionCallbackCoalescer


@pytest.mark.asyncio
async def test_callback_coalescer_runs_one_active_callback_and_one_rerun() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def callback(conversation_id: str) -> None:
        calls.append(conversation_id)
        if len(calls) == 1:
            started.set()
            await release.wait()

    coalescer = PostProjectionCallbackCoalescer(
        callback,
        max_entries=2,
        worker_count=1,
    )
    await coalescer.start()
    assert coalescer.enqueue("conversation-1")
    await started.wait()
    assert coalescer.enqueue("conversation-1")
    assert coalescer.enqueue("conversation-1")
    release.set()
    await coalescer.stop()

    assert calls == ["conversation-1", "conversation-1"]
    assert coalescer.pending_count == 0
    assert coalescer.active_count == 0


@pytest.mark.asyncio
async def test_callback_coalescer_observes_failures_without_unhandled_tasks() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def callback(_conversation_id: str) -> None:
        raise RuntimeError("callback failed")

    try:
        coalescer = PostProjectionCallbackCoalescer(
            callback,
            max_entries=1,
            worker_count=1,
        )
        await coalescer.start()
        assert coalescer.enqueue("conversation-1")
        await coalescer.stop()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert unhandled == []


@pytest.mark.asyncio
async def test_callback_coalescer_bounded_stop_cancels_stuck_callback() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def callback(_conversation_id: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    coalescer = PostProjectionCallbackCoalescer(
        callback,
        max_entries=1,
        worker_count=1,
    )
    await coalescer.start()
    assert coalescer.enqueue("conversation-1")
    await started.wait()

    await coalescer.stop(drain_timeout_seconds=0.01)

    assert cancelled.is_set()
    assert coalescer.pending_count == 0
    assert coalescer.active_count == 0
