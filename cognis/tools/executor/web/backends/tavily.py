"""Tavily web backend — search, extract, crawl, map, research.

Uses the Tavily REST API via raw httpx calls (no SDK dependency).
API docs: https://docs.tavily.com/documentation/api-reference
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.web.backends.formatting import (
    build_crawl_tool_result,
    build_search_tool_result,
)
from cognis.tools.executor.web.backends.search_intent import (
    domain_allowed,
    intent_metadata,
    result_type,
    search_mode,
    semantic_score,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.tavily.com"


def _should_trip_tavily(exc: Exception) -> bool:
    """Trip only on availability failures, not caller-side 4xx errors."""
    if isinstance(exc, httpx.TimeoutException | httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return True


_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    should_trip=_should_trip_tavily,
)


class TavilyBackend:
    """Tavily API backend for web search, extraction, crawling, and research."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def fetch(
        self,
        url: str,
        *,
        output_format: str = "markdown",
        timeout: int = 30,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Fetch URL content using Tavily Extract API."""
        opts = options or {}
        body: dict[str, Any] = {
            "urls": [url],
            "format": "text" if output_format == "text" else "markdown",
        }
        if opts.get("extract_depth"):
            body["extract_depth"] = opts["extract_depth"]
        if opts.get("query"):
            body["query"] = opts["query"]
        if opts.get("chunks_per_source"):
            body["chunks_per_source"] = opts["chunks_per_source"]
        if opts.get("include_images"):
            body["include_images"] = opts["include_images"]

        result = await self._safe_call("/extract", body, timeout=timeout)
        if isinstance(result, ToolResult):
            return result

        # Parse extract response
        results_list = result.get("results", [])
        if not results_list:
            failed = result.get("failed_results", [])
            if failed:
                reason = failed[0].get("error", "Unknown error")
                return ToolResult(output=f"Extraction failed: {reason}", is_error=True)
            return ToolResult(output="No content extracted.", is_error=True)

        content = results_list[0].get("raw_content", results_list[0].get("content", ""))
        return ToolResult(output=content)

    async def search(
        self,
        query: str,
        *,
        num_results: int = 8,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Search the web using Tavily Search API."""
        if not query:
            return ToolResult(output="No search query provided.", is_error=True)

        opts = options or {}
        requested_mode = search_mode(opts)
        preferred_type = result_type(opts)
        effective_mode = requested_mode
        native_support = requested_mode != "videos"
        degraded_reason = None
        if requested_mode == "videos":
            effective_mode = "web"
            degraded_reason = "Tavily has no native video search; used general web search fallback."
        body: dict[str, Any] = {
            "query": query,
            "max_results": min(num_results, 20),
        }
        if requested_mode == "news":
            body["topic"] = "news"
        elif "search_mode" in opts:
            body["topic"] = "general"

        # Map all supported Tavily search parameters
        _set_if(body, opts, "search_depth")  # basic, advanced, fast, ultra-fast
        if "search_mode" not in opts:
            _set_if(body, opts, "topic")  # general, news, finance
        _set_if(body, opts, "include_answer")
        _set_if(body, opts, "include_raw_content")
        _set_if(body, opts, "include_images")
        _set_if(body, opts, "include_image_descriptions")
        _set_if(body, opts, "include_favicon")
        _set_if(body, opts, "include_domains")
        _set_if(body, opts, "exclude_domains")
        _set_if(body, opts, "country")
        _set_if(body, opts, "days")
        _set_if(body, opts, "time_range")  # day, week, month, year
        _set_if(body, opts, "start_date")
        _set_if(body, opts, "end_date")
        _set_if(body, opts, "auto_parameters")
        _set_if(body, opts, "chunks_per_source")
        _set_if(body, opts, "exact_match")
        _set_if(body, opts, "include_usage")
        if requested_mode == "images":
            body["include_images"] = True

        result = await self._safe_call("/search", body)
        if isinstance(result, ToolResult):
            return result

        metadata = intent_metadata(
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            preferred_result_type=preferred_type,
            native_mode_support=native_support,
            degraded_reason=degraded_reason,
        )
        metadata["requested_time_range"] = str(opts.get("time_range") or "any").lower()
        return _format_tavily_search(
            result,
            image_only=requested_mode == "images",
            preferred_type=preferred_type,
            options=opts,
            metadata=metadata,
        )

    async def crawl(
        self,
        url: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Crawl a website using Tavily Crawl API."""
        opts = options or {}
        body: dict[str, Any] = {"url": url}

        _set_if(body, opts, "max_depth")
        _set_if(body, opts, "max_breadth")
        _set_if(body, opts, "limit")
        _set_if(body, opts, "instructions")
        _set_if(body, opts, "select_paths")
        _set_if(body, opts, "select_domains")
        _set_if(body, opts, "exclude_paths")
        _set_if(body, opts, "exclude_domains")
        _set_if(body, opts, "allow_external")
        _set_if(body, opts, "extract_depth")
        _set_if(body, opts, "format")
        _set_if(body, opts, "include_images")

        result = await self._safe_call("/crawl", body, timeout=120)
        if isinstance(result, ToolResult):
            return result

        return _format_tavily_crawl(result)

    async def map_site(
        self,
        url: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Map a website's structure using Tavily Map API."""
        opts = options or {}
        body: dict[str, Any] = {"url": url}

        _set_if(body, opts, "max_depth")
        _set_if(body, opts, "max_breadth")
        _set_if(body, opts, "limit")
        _set_if(body, opts, "instructions")
        _set_if(body, opts, "select_paths")
        _set_if(body, opts, "select_domains")
        _set_if(body, opts, "exclude_paths")
        _set_if(body, opts, "exclude_domains")
        _set_if(body, opts, "allow_external")
        _set_if(body, opts, "timeout")

        result = await self._safe_call("/map", body, timeout=150)
        if isinstance(result, ToolResult):
            return result

        urls = result.get("results", [])
        base = result.get("base_url", url)
        lines = [f"Site map for {base} ({len(urls)} URLs found):", ""]
        for u in urls:
            lines.append(f"  {u}")
        return ToolResult(output="\n".join(lines))

    async def research(
        self,
        query: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Perform deep research using Tavily Research API."""
        opts = options or {}
        body: dict[str, Any] = {"input": query}
        _set_if(body, opts, "model")  # mini, pro, auto

        result = await self._safe_call("/research", body, timeout=300)
        if isinstance(result, ToolResult):
            return result

        # Research returns a structured report
        content = result.get("content", result.get("output", ""))
        if isinstance(content, str):
            return ToolResult(output=content)
        return ToolResult(output=str(content))

    async def _post(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Make a POST request to Tavily API.

        Raises on failure so the circuit breaker can count errors.
        """
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{_BASE_URL}{endpoint}",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]

    async def _safe_call(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        timeout: int = 60,
    ) -> dict[str, Any] | ToolResult:
        """Call Tavily API through circuit breaker, converting errors to ToolResult."""
        try:
            return await _breaker.call(lambda: self._post(endpoint, body, timeout=timeout))
        except CircuitBreakerError:
            return ToolResult(
                output="Tavily unavailable (circuit breaker open). Try again later.",
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(output=f"Tavily request timed out after {timeout}s.", is_error=True)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except Exception:
                detail = exc.response.text
            return ToolResult(output=f"Tavily API error (HTTP {status}): {detail}", is_error=True)
        except httpx.RequestError:
            return ToolResult(output="Tavily request failed.", is_error=True)


def _set_if(body: dict[str, Any], opts: dict[str, Any], key: str) -> None:
    """Set body[key] = opts[key] if the key exists in opts."""
    if key in opts:
        body[key] = opts[key]


def _format_tavily_search(
    data: dict[str, Any],
    *,
    image_only: bool = False,
    preferred_type: str | None = None,
    options: dict[str, Any] | None = None,
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    """Format Tavily search response into readable output."""
    answer = data.get("answer")
    results = data.get("results", [])
    images = _format_tavily_images(data.get("images"))
    if image_only and not images:
        result = build_search_tool_result(answer=None, results=[], metadata=metadata)
        result.output = "No image results found."
        return result
    opts = options or {}
    formatted_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
            "score": r.get("score"),
            "published_date": (
                r.get("published_date")
                or r.get("publishedDate")
                or r.get("published_at")
                or r.get("date")
            ),
            "cognis_score": semantic_score(
                preferred_type,
                str(r.get("url", "")),
                str(r.get("title", "")),
            ),
        }
        for r in results
    ]
    formatted_results = [
        row for row in formatted_results if domain_allowed(str(row.get("url") or ""), opts)
    ]
    formatted_results.sort(
        key=lambda row: (
            -float(row["cognis_score"]) if isinstance(row.get("cognis_score"), int | float) else 0.0
        )
    )
    return build_search_tool_result(
        answer=str(answer) if isinstance(answer, str) and not image_only else None,
        results=[] if image_only else formatted_results,
        images=images,
        metadata=metadata,
    )


def _format_tavily_images(value: object) -> list[dict[str, object]]:
    """Normalize Tavily's URL and object image response variants."""
    if not isinstance(value, list):
        return []
    images: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, str) and item:
            images.append({"url": item, "source": "tavily_search"})
        elif isinstance(item, dict):
            url = item.get("url") or item.get("image_url") or item.get("image")
            if isinstance(url, str) and url:
                images.append(
                    {
                        "url": url,
                        "alt": item.get("description") or item.get("title"),
                        "caption": item.get("description") or item.get("title"),
                        "source": "tavily_search",
                        "source_page_url": item.get("source_url") or item.get("url"),
                    }
                )
    return images


def _format_tavily_crawl(data: dict[str, Any]) -> ToolResult:
    """Format Tavily crawl response."""
    results = data.get("results", [])
    if not results:
        return ToolResult(output="No pages crawled.")

    pages = [
        {
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "content": r.get("raw_content", r.get("content", "")),
            "is_error": False,
        }
        for r in results
        if isinstance(r, dict)
    ]
    root_url = str(data.get("base_url") or pages[0].get("url") or "site")
    return build_crawl_tool_result(root_url=root_url, pages=pages)
