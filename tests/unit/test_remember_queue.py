from __future__ import annotations

import asyncio

import pytest

from cognis.core.remember_queue import RememberRetryQueue


class _Worker:
    def __init__(self) -> None:
        self.calls = 0

    async def remember(self, **kwargs: object) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_remember_queue_processes_items() -> None:
    worker = _Worker()
    queue = RememberRetryQueue(worker, max_depth=10, max_concurrent=1)
    await queue.start()
    await queue.enqueue({"session_id": "s1", "messages": []})
    await asyncio.sleep(0.3)
    await queue.stop()

    assert worker.calls >= 1
