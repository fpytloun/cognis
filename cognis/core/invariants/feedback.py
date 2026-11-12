"""Recover original pending feedback intents, never infer execution from chat idleness."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cognis.core.feedback.service import FeedbackService

logger = logging.getLogger(__name__)


class FeedbackReconciler:
    """Controller-owned cancellable maintenance; database leases fence replicas."""

    def __init__(self, feedback: FeedbackService) -> None:
        self.feedback = feedback
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="feedback-reconciler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.feedback.submissions.reconcile()
                await self.feedback.payloads.collect_staged(datetime.now(UTC) - timedelta(days=1))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Maintenance is isolated, but reports the category without content.
                logger.warning("Feedback maintenance failed category=%s", type(exc).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=30)
