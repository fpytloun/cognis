"""Content-free dependency identity for Intaris HTTP failures."""

from __future__ import annotations

import httpx


class IntarisDependencyError(Exception):
    """A content-free Intaris dependency failure, not an LLM provider failure."""

    operation: str
    transient: bool
    status_code: int | None = None


class IntarisHTTPError(httpx.HTTPStatusError, IntarisDependencyError):
    """An exhausted Intaris request, retaining its response and original cause."""

    def __init__(self, error: httpx.HTTPStatusError, *, operation: str) -> None:
        self.operation = operation
        self.status_code = error.response.status_code
        self.transient = self.status_code == 429 or 500 <= self.status_code < 600
        super().__init__(
            f"Intaris {operation} failed (HTTP {self.status_code}).",
            request=error.request,
            response=error.response,
        )


class IntarisTransportError(httpx.RequestError, IntarisDependencyError):
    """An exhausted Intaris transport request, without exposing its URL."""

    def __init__(self, error: httpx.RequestError, *, operation: str) -> None:
        self.operation = operation
        self.transient = True
        super().__init__(
            f"Intaris {operation} failed ({type(error).__name__}).",
            request=error.request,
        )
