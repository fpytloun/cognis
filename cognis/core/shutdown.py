"""Controller drain coordination before server transport shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from time import monotonic
from typing import Any, Protocol, runtime_checkable

from cognis.logging import get_logger

BEGIN_DRAIN_TIMEOUT_SECONDS = 2.0
CANCEL_CLEANUP_TIMEOUT_SECONDS = 0.1

logger = get_logger(__name__)


@runtime_checkable
class ExecutorToolDrainer(Protocol):
    """Executor transport settlement interface required during shutdown."""

    async def drain_tool_calls(self, *, timeout_seconds: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class ShutdownCallbacks:
    """Public application lifecycle callbacks used by the server boundary."""

    begin_drain: Callable[[], Awaitable[None]]
    drain_turns: Callable[[float, float], Awaitable[dict[str, int]]]
    drain_tool_calls: Callable[[float], Awaitable[bool]]


class ShutdownCoordinator:
    """Run one bounded application drain before transports close."""

    def __init__(self) -> None:
        self._callbacks: ShutdownCallbacks | None = None
        self._drain_timeout_seconds = 0.0
        self._cancel_timeout_seconds = 0.0
        self._task: asyncio.Task[None] | None = None
        self._force = asyncio.Event()
        self._retained_tasks: set[asyncio.Future[Any]] = set()

    def configure(
        self,
        callbacks: ShutdownCallbacks,
        *,
        drain_timeout_seconds: float,
        cancel_timeout_seconds: float,
    ) -> None:
        if self._callbacks is not None:
            raise RuntimeError("shutdown coordinator is already configured")
        self._callbacks = callbacks
        self._drain_timeout_seconds = max(0.0, drain_timeout_seconds)
        self._cancel_timeout_seconds = max(0.0, cancel_timeout_seconds)

    def force(self) -> None:
        """Stop waiting for graceful settlement."""

        self._force.set()

    async def drain(self) -> None:
        """Start or join the single drain operation."""

        if self._callbacks is None:
            return
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="controller-early-drain",
            )
        await asyncio.shield(self._task)

    async def _run(self) -> None:
        callbacks = self._callbacks
        if callbacks is None:
            return
        await self._run_stage(
            "begin_drain",
            callbacks.begin_drain(),
            BEGIN_DRAIN_TIMEOUT_SECONDS,
        )
        if self._force.is_set():
            return
        deadline = monotonic() + self._drain_timeout_seconds + self._cancel_timeout_seconds
        if self._remaining(deadline) <= 0:
            return
        await self._run_stage(
            "drain_turns",
            callbacks.drain_turns(
                min(self._drain_timeout_seconds, self._remaining(deadline)),
                min(self._cancel_timeout_seconds, self._remaining(deadline)),
            ),
            self._remaining(deadline),
        )
        if self._force.is_set():
            return
        remaining = self._remaining(deadline)
        if remaining > 0:
            await self._run_stage(
                "drain_tool_calls",
                callbacks.drain_tool_calls(remaining),
                remaining,
            )

    async def _run_stage(
        self,
        stage: str,
        operation: Awaitable[Any],
        timeout: float,
    ) -> None:
        try:
            await self._wait_or_force(stage, operation, timeout)
        except Exception:
            logger.exception(
                "shutdown stage failed",
                extra={"extra_data": {"stage": stage}},
            )

    async def _wait_or_force(
        self,
        stage: str,
        operation: Awaitable[Any],
        timeout: float,
    ) -> Any:
        task = asyncio.ensure_future(operation)
        force_waiter = asyncio.create_task(self._force.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, force_waiter},
                timeout=max(0.0, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            task.cancel()
            cancelled, _pending = await asyncio.wait(
                {task},
                timeout=CANCEL_CLEANUP_TIMEOUT_SECONDS,
            )
            if task in cancelled:
                await asyncio.gather(task, return_exceptions=True)
            else:
                self._retain_task(stage, task)
                logger.warning(
                    "shutdown stage ignored cancellation",
                    extra={"extra_data": {"stage": stage}},
                )
            return None
        finally:
            force_waiter.cancel()
            await asyncio.gather(force_waiter, return_exceptions=True)

    def _retain_task(self, stage: str, task: asyncio.Future[Any]) -> None:
        self._retained_tasks.add(task)
        task.add_done_callback(partial(self._observe_retained_task, stage))

    def _observe_retained_task(
        self,
        stage: str,
        task: asyncio.Future[Any],
    ) -> None:
        self._retained_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "shutdown stage failed after cancellation timeout",
                exc_info=(
                    type(exception),
                    exception,
                    exception.__traceback__,
                ),
                extra={"extra_data": {"stage": stage}},
            )

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - monotonic())
