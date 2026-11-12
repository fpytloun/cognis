"""Stable usage errors shared by SQL, HTTP and controller-tool adapters."""

from __future__ import annotations


class UsageQueryError(ValueError):
    """An actionable bounded-query failure, not a zero-valued accounting result."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
