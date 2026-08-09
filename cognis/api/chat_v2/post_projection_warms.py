"""Bounded revision ownership for post-projection snapshot warms."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PostProjectionWarmReservation:
    """One reversible forced-warm marker update."""

    conversation_id: str
    revision: int
    previous_revision: int | None


class PostProjectionWarmRevisions:
    """Fence forced warm completion with monotonic per-conversation revisions."""

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1 or max_entries > 4096:
            raise ValueError("max_entries must be in 1..4096")
        self._max_entries = max_entries
        self._revisions: OrderedDict[str, int] = OrderedDict()
        self._next_revision = 0

    def current(self, conversation_id: str) -> int | None:
        return self._revisions.get(conversation_id)

    def reserve(self, conversation_id: str) -> PostProjectionWarmReservation | None:
        """Reserve bounded marker capacity without evicting accepted work."""

        previous_revision = self._revisions.get(conversation_id)
        if previous_revision is None and len(self._revisions) >= self._max_entries:
            return None
        self._next_revision += 1
        revision = self._next_revision
        self._revisions.pop(conversation_id, None)
        self._revisions[conversation_id] = revision
        return PostProjectionWarmReservation(
            conversation_id=conversation_id,
            revision=revision,
            previous_revision=previous_revision,
        )

    def admit(
        self,
        conversation_id: str,
        enqueue: Callable[[str], bool],
    ) -> bool:
        """Publish a marker only for work accepted by the synchronous warmer."""

        reservation = self.reserve(conversation_id)
        if reservation is None:
            return False
        try:
            accepted = enqueue(conversation_id)
        except BaseException:
            self.cancel(reservation)
            raise
        if accepted:
            return True
        self.cancel(reservation)
        return False

    def cancel(self, reservation: PostProjectionWarmReservation) -> bool:
        """Roll back a rejected admission without changing newer marker state."""

        if self._revisions.get(reservation.conversation_id) != reservation.revision:
            return False
        if reservation.previous_revision is None:
            self._revisions.pop(reservation.conversation_id, None)
        else:
            self._revisions[reservation.conversation_id] = reservation.previous_revision
        return True

    def complete(self, conversation_id: str, revision: int | None) -> bool:
        """Clear only the revision owned by the completing callback."""

        if revision is None or self._revisions.get(conversation_id) != revision:
            return False
        self._revisions.pop(conversation_id, None)
        return True

    def clear(self) -> None:
        self._revisions.clear()


__all__ = [
    "PostProjectionWarmReservation",
    "PostProjectionWarmRevisions",
]
