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
        body: dict[str, Any] = {
            "query": query,
            "max_results": min(num_results, 20),
        }

        # Map all supported Tavily search parameters
        _set_if(body, opts, "search_depth")  # basic, advanced, fast, ultra-fast
        _set_if(body, opts, "topic")  # general, news, finance
        _set_if(body, opts, "include_answer")
        _set_if(body, opts, "include_raw_content")
        _set_if(body, opts, "include_images")
        _set_if(body, opts, "include_image_descriptions")
        _set_if(body, opts, "include_domains")
        _set_if(body, opts, "exclude_domains")
        _set_if(body, opts, "country")
        _set_if(body, opts, "days")
        _set_if(body, opts, "time_range")  # day, week, month, year
        _set_if(body, opts, "auto_parameters")
        _set_if(body, opts, "chunks_per_source")

        result = await self._safe_call("/search", body)
        if isinstance(result, ToolResult):
            return result

        return _format_tavily_search(result)

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


def _format_tavily_search(data: dict[str, Any]) -> ToolResult:
    """Format Tavily search response into readable output."""
    lines: list[str] = []

    answer = data.get("answer")
    if answer:
        lines.append(f"Answer: {answer}")
        lines.append("")

    results = data.get("results", [])
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        score = r.get("score")
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if score is not None:
            lines.append(f"    Relevance: {score:.2f}")
        if content:
            lines.append(f"    {content}")
        lines.append("")

    if not results and not answer:
        return ToolResult(output="No search results found.")

    return ToolResult(output="\n".join(lines))


def _format_tavily_crawl(data: dict[str, Any]) -> ToolResult:
    """Format Tavily crawl response."""
    results = data.get("results", [])
    if not results:
        return ToolResult(output="No pages crawled.")

    lines: list[str] = [f"Crawled {len(results)} pages:", ""]
    for r in results:
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("raw_content", r.get("content", ""))
        lines.append(f"--- {title} ({url}) ---")
        if content:
            # Truncate individual page content to keep output manageable
            if len(content) > 10_000:
                content = content[:10_000] + "\n[content truncated]"
            lines.append(content)
        lines.append("")

    return ToolResult(output="\n".join(lines))
