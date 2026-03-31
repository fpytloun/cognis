"""Direct web backend — httpx fetch + DuckDuckGo search.

This is the zero-config default backend. No API key required.

NOTE: DuckDuckGo search uses the ``duckduckgo-search`` library which
scrapes DDG's web interface. It has no official API contract and may
break when DDG changes their frontend. If DDG is unavailable, the
search returns an actionable error suggesting Tavily or Brave.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.web.headers import (
    clamp_timeout,
    fetch_with_retry,
    format_response,
)

logger = logging.getLogger(__name__)

_fetch_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
_search_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)


class DirectBackend:
    """Direct HTTP fetch + DuckDuckGo search (no API key needed)."""

    async def fetch(
        self,
        url: str,
        *,
        output_format: str = "markdown",
        timeout: int = 30,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Fetch a URL using httpx with browser-like headers."""
        timeout = clamp_timeout(timeout)

        try:
            result = await _fetch_breaker.call(lambda: fetch_with_retry(url, timeout=timeout))
        except CircuitBreakerError:
            return ToolResult(
                output="Web fetch unavailable (circuit breaker open). Try again later.",
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(output=f"Request timed out after {timeout}s.", is_error=True)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = exc.response.reason_phrase or "Unknown"
            return ToolResult(output=f"HTTP {status}: {reason}", is_error=True)
        except httpx.RequestError:
            return ToolResult(output="Web fetch request failed.", is_error=True)

        if isinstance(result, ToolResult):
            return result

        # result is an httpx.Response
        return ToolResult(output=format_response(result, output_format))

    async def search(
        self,
        query: str,
        *,
        num_results: int = 8,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Search the web using DuckDuckGo."""
        if not query:
            return ToolResult(output="No search query provided.", is_error=True)

        opts = options or {}
        region = opts.get("region", "wt-wt")
        safesearch = opts.get("safesearch", "moderate")
        timelimit = opts.get("timelimit")  # d, w, m, y

        try:
            return await _search_breaker.call(
                lambda: _ddg_search(
                    query,
                    max_results=num_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
            )
        except CircuitBreakerError:
            return ToolResult(
                output="Web search unavailable (circuit breaker open). Try again later.",
                is_error=True,
            )
        except Exception as exc:
            logger.warning("web: DuckDuckGo search failed: %s", type(exc).__name__)
            return ToolResult(
                output=(
                    "DuckDuckGo search failed. "
                    "Consider configuring Tavily or Brave backend "
                    "in Settings for more reliable web search."
                ),
                is_error=True,
            )


async def _ddg_search(
    query: str,
    *,
    max_results: int = 8,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    timelimit: str | None = None,
) -> ToolResult:
    """Execute DuckDuckGo search in a thread (the library is sync)."""
    import asyncio

    def _sync_search() -> list[dict[str, str]]:
        from duckduckgo_search import DDGS  # type: ignore[import-untyped]

        with DDGS() as ddgs:
            results = list(
                ddgs.text(
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
            )
        return results

    results = await asyncio.to_thread(_sync_search)

    if not results:
        return ToolResult(output="No search results found.")

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("href", r.get("link", ""))
        body = r.get("body", r.get("snippet", ""))
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if body:
            lines.append(f"    {body}")
        lines.append("")

    return ToolResult(output="\n".join(lines))
