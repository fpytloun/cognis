"""Periodic retention maintenance for saved tool outputs."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from time import monotonic
from typing import Any

from cognis.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolOutputMaintenanceResult:
    """Summary of one tool-output maintenance pass."""

    expired_deleted: int
    size_cap_deleted: int
    cleanup_failed: bool
    size_cap_failed: bool
    duration_seconds: float


class ToolOutputMaintenanceService:
    """Periodically enforce tool-output TTL and storage-size limits."""

    def __init__(self, tool_output_store: Any, *, interval_seconds: float = 300.0) -> None:
        self._tool_output_store = tool_output_store
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start an immediate background pass followed by periodic maintenance."""

        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="tool-output-maintenance",
        )

    async def stop(self) -> None:
        """Cancel and await the maintenance loop."""

        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> ToolOutputMaintenanceResult:
        """Run one failure-isolated retention and size-cap pass."""

        started_at = monotonic()
        expired_deleted = 0
        size_cap_deleted = 0
        cleanup_failed = False
        size_cap_failed = False

        try:
            expired_deleted = await self._tool_output_store.cleanup_expired()
        except Exception:
            cleanup_failed = True
            logger.warning("tool output TTL maintenance failed", exc_info=True)

        try:
            size_cap_deleted = await self._tool_output_store.enforce_size_cap()
        except Exception:
            size_cap_failed = True
            logger.warning("tool output size-cap maintenance failed", exc_info=True)

        duration_seconds = monotonic() - started_at
        if expired_deleted or size_cap_deleted or duration_seconds >= 1.0:
            logger.info(
                "tool output maintenance completed",
                extra={
                    "extra_data": {
                        "expired_deleted": expired_deleted,
                        "size_cap_deleted": size_cap_deleted,
                        "cleanup_failed": cleanup_failed,
                        "size_cap_failed": size_cap_failed,
                        "duration_seconds": round(duration_seconds, 3),
                    }
                },
            )

        return ToolOutputMaintenanceResult(
            expired_deleted=expired_deleted,
            size_cap_deleted=size_cap_deleted,
            cleanup_failed=cleanup_failed,
            size_cap_failed=size_cap_failed,
            duration_seconds=duration_seconds,
        )

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning("tool output maintenance failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue
