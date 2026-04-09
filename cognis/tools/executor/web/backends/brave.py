"""Brave Search web backend.

Uses the Brave Web Search API via raw httpx calls.
API docs: https://api-dashboard.search.brave.com/app/documentation/web-search

Brave is search-only — it has no URL fetch/extract endpoint.
When used as the default backend, web_fetch falls back to direct.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.search.brave.com/res/v1/web/search"


def _should_trip_brave(exc: Exception) -> bool:
    """Trip only on availability failures, not caller-side 4xx errors."""
    if isinstance(exc, httpx.TimeoutException | httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return True


_breaker = CircuitBreaker(
    failure_threshold=5,
    recovery_timeout=30.0,
    should_trip=_should_trip_brave,
)


class BraveBackend:
    """Brave Search API backend (search only)."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(
        self,
        query: str,
        *,
        num_results: int = 8,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Search the web using Brave Search API."""
        if not query:
            return ToolResult(output="No search query provided.", is_error=True)

        opts = options or {}
        params: dict[str, Any] = {
            "q": query,
            "count": min(num_results, 20),
        }

        # Map all supported Brave search parameters
        _set_if(params, opts, "country")
        _set_if(params, opts, "search_lang")
        _set_if(params, opts, "ui_lang")
        _set_if(params, opts, "offset")
        _set_if(params, opts, "safesearch")
        _set_if(params, opts, "freshness")  # pd, pw, pm, py, or date range
        _set_if(params, opts, "text_decorations")
        _set_if(params, opts, "spellcheck")
        _set_if(params, opts, "extra_snippets")
        _set_if(params, opts, "goggles_id")
        _set_if(params, opts, "units")
        _set_if(params, opts, "result_filter")

        try:
            result = await _breaker.call(lambda: self._get(params))
        except CircuitBreakerError:
            return ToolResult(
                output="Brave Search unavailable (circuit breaker open). Try again later.",
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(output="Brave Search request timed out.", is_error=True)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = exc.response.reason_phrase or "Unknown"
            if status == 429:
                return ToolResult(
                    output="Brave Search rate limit exceeded. Try again later.",
                    is_error=True,
                )
            return ToolResult(
                output=f"Brave Search API error (HTTP {status}): {reason}",
                is_error=True,
            )
        except httpx.RequestError:
            return ToolResult(output="Brave Search request failed.", is_error=True)

        return _format_brave_results(result)

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Make a GET request to Brave Search API.

        Raises on failure so the circuit breaker can count errors.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                _BASE_URL,
                params=params,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self.api_key,
                },
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]


def _set_if(params: dict[str, Any], opts: dict[str, Any], key: str) -> None:
    """Set params[key] = opts[key] if the key exists in opts."""
    if key in opts:
        params[key] = opts[key]


def _format_brave_results(data: dict[str, Any]) -> ToolResult:
    """Format Brave Search API response into readable output."""
    web = data.get("web", {})
    results = web.get("results", [])

    if not results:
        return ToolResult(output="No search results found.")

    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        description = r.get("description", "")
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if description:
            lines.append(f"    {description}")

        extra = r.get("extra_snippets", [])
        if extra:
            for snippet in extra[:3]:
                lines.append(f"    > {snippet}")
        lines.append("")

    return ToolResult(output="\n".join(lines))
