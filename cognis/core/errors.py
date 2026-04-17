"""Core orchestration error types."""

from __future__ import annotations


class ImmutablePrefixUnavailable(RuntimeError):
    """Raised when the immutable prefix cannot be established for a turn."""

    def __init__(self, message: str, *, reason: str = "unavailable") -> None:
        super().__init__(message)
        self.reason = reason
