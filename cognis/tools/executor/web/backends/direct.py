"""Direct web backend — httpx fetch + DuckDuckGo search.

This is the zero-config default backend. No API key required.

NOTE: DuckDuckGo search uses the ``duckduckgo-search`` library which
scrapes DDG's web interface. It has no official API contract and may
break when DDG changes their frontend. If DDG is unavailable, the
search returns an actionable error suggesting Tavily or Brave.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.web.backends.formatting import build_search_tool_result
from cognis.tools.executor.web.backends.reddit import fetch_reddit_thread
from cognis.tools.executor.web.headers import (
    clamp_timeout,
    fetch_with_retry,
    format_response_result,
)

logger = logging.getLogger(__name__)
_DDG_REQUEST_TIMEOUT_SECONDS = 15

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
            reddit_result = await fetch_reddit_thread(
                url,
                output_format=output_format,
                timeout=timeout,
            )
        except (httpx.HTTPError, ValueError):
            logger.debug("web: Reddit JSON adapter failed; falling back to direct HTTP")
        else:
            if reddit_result is not None:
                return reddit_result

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
        response_url = str(getattr(result, "url", "") or "")
        final_url = response_url if response_url.startswith(("http://", "https://")) else url
        # Extraction is synchronous and CPU-heavy. Keep it off the executor
        # event loop so websocket pings and heartbeats remain responsive.
        return await asyncio.to_thread(
            format_response_result,
            result,
            output_format,
            source_url=final_url,
            options=options,
        )

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
        region = opts.get("region", "us-en")
        safesearch = opts.get("safesearch", "moderate")
        timelimit = opts.get("timelimit")  # d, w, m, y
        include_images = opts.get("include_images") is True
        image_limit = _clamp_image_limit(opts.get("image_limit"), default=10)

        try:
            return await _search_breaker.call(
                lambda: _ddg_search(
                    query,
                    max_results=num_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    include_images=include_images,
                    image_limit=image_limit,
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
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: str | None = None,
    include_images: bool = False,
    image_limit: int = 10,
) -> ToolResult:
    """Execute DuckDuckGo search in a thread (the library is sync)."""
    import asyncio

    def _sync_search() -> tuple[list[dict[str, str]], list[dict[str, object]]]:
        from ddgs import DDGS

        ddgs = DDGS(timeout=_DDG_REQUEST_TIMEOUT_SECONDS)
        text_results = list(
            ddgs.text(
                query,
                max_results=max_results,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
            )
        )
        image_results: list[dict[str, object]] = []
        if include_images:
            image_results = list(
                ddgs.images(
                    query,
                    max_results=image_limit,
                    region=region,
                    safesearch=safesearch,
                )
            )
        return text_results, image_results

    results, raw_images = await asyncio.to_thread(_sync_search)

    if not results and not raw_images:
        return ToolResult(output="No search results found.")

    formatted_results: list[dict[str, object]] = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "snippet": r.get("body", r.get("snippet", "")),
        }
        for r in results
    ]
    images = [
        {
            "url": image.get("image") or image.get("thumbnail"),
            "alt": image.get("title"),
            "caption": image.get("title"),
            "source": "duckduckgo_image_search",
            "source_page_url": image.get("url"),
        }
        for image in raw_images
        if isinstance(image.get("image") or image.get("thumbnail"), str)
    ]
    return build_search_tool_result(answer=None, results=formatted_results, images=images)


def _clamp_image_limit(value: object, *, default: int) -> int:
    try:
        return min(max(int(value), 0), 50)
    except (TypeError, ValueError):
        return default
