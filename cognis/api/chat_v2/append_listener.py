"""Deadline-safe local fast path for durable Intaris append notifications."""

from __future__ import annotations

from typing import Any

from cognis.logging import get_logger
from cognis.providers.guardrails.events import EventAppendNotification

logger = get_logger(__name__)


class EventAppendListenerFastPath:
    """Run ordered local admissions without awaiting database or network I/O."""

    def __init__(
        self,
        *,
        event_store: Any,
        invalidation_dispatcher: Any,
        work_materializer: Any,
    ) -> None:
        self._event_store = event_store
        self._invalidation_dispatcher = invalidation_dispatcher
        self._work_materializer = work_materializer

    async def __call__(self, notification: EventAppendNotification) -> None:
        work = None
        try:
            work = self._event_store.invalidate_append_local(notification)
        except Exception:
            logger.warning("chat_v2: local append invalidation failed", exc_info=True)
        if work is not None:
            try:
                self._invalidation_dispatcher.enqueue(work)
            except Exception:
                logger.warning(
                    "chat_v2: append invalidation admission failed",
                    exc_info=True,
                )
        try:
            accepted = self._work_materializer.enqueue_append(notification)
            if not accepted:
                logger.warning("chat_v2: Work append rejected during shutdown")
        except Exception:
            logger.warning("chat_v2: Work append admission failed", exc_info=True)


__all__ = ["EventAppendListenerFastPath"]
