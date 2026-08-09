from __future__ import annotations

from cognis.providers.base import MemoryProvider


class RememberOutcomeUnknownError(RuntimeError):
    """The remember request may have been accepted and must not be retried."""


__all__ = ["MemoryProvider", "RememberOutcomeUnknownError"]
