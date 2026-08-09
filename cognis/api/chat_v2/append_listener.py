"""Deadline-safe local fast path for durable Intaris append notifications."""

from __future__ import annotations

from collections.abc import Callable
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
        pending_warms: Any,
        invalidation_dispatcher: Any,
        work_materializer: Any,
        on_mapping_size: Callable[[int], None],
        on_mapping_overflow: Callable[[], None],
    ) -> None:
        self._event_store = event_store
        self._pending_warms = pending_warms
        self._invalidation_dispatcher = invalidation_dispatcher
        self._work_materializer = work_materializer
        self._on_mapping_size = on_mapping_size
        self._on_mapping_overflow = on_mapping_overflow

    async def __call__(self, notification: EventAppendNotification) -> None:
        work = None
        try:
            work = self._event_store.invalidate_append_local(notification)
        except Exception:
            logger.warning("chat_v2: local append invalidation failed", exc_info=True)
        try:
            session_token = (
                work.session_token
                if work is not None
                else self._event_store.session_token("intaris", notification.session_id)
            )
            overflowed = self._pending_warms.put(
                session_token,
                (
                    notification.session_id,
                    notification.last_seq,
                    notification.authority.user_email,
                ),
            )
            if overflowed:
                self._on_mapping_overflow()
            self._on_mapping_size(len(self._pending_warms))
        except Exception:
            logger.warning("chat_v2: append warm mapping failed", exc_info=True)
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
