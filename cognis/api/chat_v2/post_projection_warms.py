"""Bounded revision ownership for post-projection snapshot warms."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cognis.api.chat_v2.work_overview_metrics import WORK_OVERVIEW_METRICS

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _CallbackState:
    active: bool = False
    rerun: bool = False


class PostProjectionCallbackCoalescer:
    """Bound post-projection callbacks by conversation with one active rerun."""

    def __init__(
        self,
        callback: Callable[[str], Awaitable[None]],
        *,
        max_entries: int,
        worker_count: int = 4,
    ) -> None:
        if max_entries < 1 or max_entries > 4096:
            raise ValueError("max_entries must be in 1..4096")
        if worker_count < 1 or worker_count > 32:
            raise ValueError("worker_count must be in 1..32")
        self._callback = callback
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=max_entries)
        self._max_entries = max_entries
        self._worker_count = worker_count
        self._states: dict[str, _CallbackState] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._accepting = False

    @property
    def pending_count(self) -> int:
        return sum(not state.active for state in self._states.values())

    @property
    def active_count(self) -> int:
        return sum(state.active for state in self._states.values())

    async def start(self) -> None:
        if self._workers:
            return
        self._accepting = True
        self._workers = [
            asyncio.create_task(
                self._worker(),
                name=f"work-post-projection-{index}",
            )
            for index in range(self._worker_count)
        ]
        self._observe_size()

    def enqueue(self, conversation_id: str) -> bool:
        if not self._accepting:
            WORK_OVERVIEW_METRICS.coalescing("stopped")
            return False
        state = self._states.get(conversation_id)
        if state is not None:
            if state.active:
                state.rerun = True
                WORK_OVERVIEW_METRICS.coalescing("coalesced_active")
            else:
                WORK_OVERVIEW_METRICS.coalescing("coalesced_pending")
            self._observe_size()
            return True
        if len(self._states) >= self._max_entries:
            WORK_OVERVIEW_METRICS.coalescing("overflow")
            return False
        self._states[conversation_id] = _CallbackState()
        try:
            self._queue.put_nowait(conversation_id)
        except asyncio.QueueFull:
            self._states.pop(conversation_id, None)
            WORK_OVERVIEW_METRICS.coalescing("overflow")
            self._observe_size()
            return False
        WORK_OVERVIEW_METRICS.coalescing("accepted")
        self._observe_size()
        return True

    async def stop(self, *, drain_timeout_seconds: float = 2.0) -> None:
        if drain_timeout_seconds <= 0:
            raise ValueError("drain_timeout_seconds must be positive")
        if not self._workers:
            self._accepting = False
            return
        self._accepting = False
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_seconds)
        except TimeoutError:
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self._queue.task_done()
        else:
            for _worker in self._workers:
                await self._queue.put(None)
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._states.clear()
        self._observe_size()

    async def _worker(self) -> None:
        while True:
            conversation_id = await self._queue.get()
            if conversation_id is None:
                self._queue.task_done()
                return
            state = self._states.get(conversation_id)
            if state is None:
                self._queue.task_done()
                continue
            state.active = True
            self._observe_size()
            try:
                await self._callback(conversation_id)
            except asyncio.CancelledError:
                WORK_OVERVIEW_METRICS.callback("cancelled")
                raise
            except Exception:
                WORK_OVERVIEW_METRICS.callback("failure")
                logger.warning(
                    "Work post-projection callback failed",
                    exc_info=True,
                )
            else:
                WORK_OVERVIEW_METRICS.callback("success")
            finally:
                current = self._states.get(conversation_id)
                task = asyncio.current_task()
                cancelling = bool(task is not None and task.cancelling())
                if current is not None and current.rerun and not cancelling:
                    current.active = False
                    current.rerun = False
                    self._queue.put_nowait(conversation_id)
                else:
                    self._states.pop(conversation_id, None)
                self._queue.task_done()
                self._observe_size()

    def _observe_size(self) -> None:
        WORK_OVERVIEW_METRICS.coalescer_size(
            pending=self.pending_count,
            active=self.active_count,
        )


__all__ = ["PostProjectionCallbackCoalescer"]
