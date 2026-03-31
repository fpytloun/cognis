"""Protocols for web tool backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from cognis.models.tool import ToolResult


@runtime_checkable
class WebFetchBackend(Protocol):
    """Protocol for web content fetching backends."""

    async def fetch(
        self,
        url: str,
        *,
        output_format: str = "markdown",
        timeout: int = 30,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Fetch content from a URL.

        Args:
            url: The URL to fetch.
            output_format: Output format — "text", "markdown", or "html".
            timeout: Request timeout in seconds.
            options: Backend-specific options (e.g. Tavily extract_depth).
        """
        ...


@runtime_checkable
class WebSearchBackend(Protocol):
    """Protocol for web search backends."""

    async def search(
        self,
        query: str,
        *,
        num_results: int = 8,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Search the web.

        Args:
            query: Search query.
            num_results: Number of results to return.
            options: Backend-specific options (e.g. search_depth, topic).
        """
        ...
