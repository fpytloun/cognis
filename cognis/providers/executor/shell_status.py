"""Connection-scoped shell status reads and authenticated executor identity."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class _StatusRead:
    task: asyncio.Task[dict[str, Any]]
    waiters: int = 0


class ShellStatusReads:
    """Share overlapping reads only; never cache completed snapshots or errors."""

    def __init__(self, fetch: Callable[[bool], Awaitable[dict[str, Any]]]) -> None:
        self._fetch = fetch
        self._reads: dict[bool, _StatusRead] = {}

    async def read(self, *, include_completed: bool) -> dict[str, Any]:
        """Read an independent snapshot, cancelling RPC when its last waiter leaves."""
        read = self._reads.get(include_completed)
        if read is None or read.task.done():

            async def fetch() -> dict[str, Any]:
                return await self._fetch(include_completed)

            read = _StatusRead(asyncio.create_task(fetch(), name="executor-shell-status"))
            self._reads[include_completed] = read
        read.waiters += 1
        try:
            # A sidebar timeout must not cancel another session's status read.
            snapshot = await asyncio.shield(read.task)
            return deepcopy(snapshot)
        finally:
            read.waiters -= 1
            if read.waiters == 0:
                if self._reads.get(include_completed) is read:
                    self._reads.pop(include_completed)
                if not read.task.done():
                    read.task.cancel()
                await asyncio.gather(read.task, return_exceptions=True)


def bind_shell_status(snapshot: dict[str, Any], executor_id: str) -> dict[str, Any]:
    """Project connection identity without trusting the executor's local label.

    Standalone executors use a process-local label such as ``remote``. The
    authenticated transport owns routing identity; user/conversation ownership
    remains untouched and must still be checked by consumers.
    """
    shells = snapshot.get("shells")
    if not isinstance(shells, list):
        return snapshot
    return {
        **snapshot,
        "shells": [
            {
                **row,
                "reported_executor_id": row.get("executor_id"),
                "executor_id": executor_id,
            }
            if isinstance(row, dict)
            else row
            for row in shells
        ],
    }
