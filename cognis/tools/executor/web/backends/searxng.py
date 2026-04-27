"""SearXNG search backend.

SearXNG (https://searxng.org/) is a self-hosted privacy-respecting
metasearch engine that aggregates results from Google, Bing, DuckDuckGo,
Mojeek, Qwant, and many others. It exposes a JSON API at ``/search``
when ``search.formats`` includes ``json`` in the instance config.

Configuration: set ``web.searxng_url`` to the instance URL (e.g.
``http://localhost:8888``). Optionally pin engines/categories/language
via ``web.searxng_engines`` / ``web.searxng_categories`` /
``web.searxng_language`` settings. No API key is required; cognis
reaches the instance over plain HTTP/HTTPS.

The backend follows the same circuit-breaker conventions as the Brave
backend: trips on 5xx, timeout, and network errors; lets the caller
surface 4xx (mis-config, rate limit) as actionable errors without
blowing the breaker.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.web.backends.formatting import build_search_tool_result

logger = logging.getLogger(__name__)

_SEARXNG_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "cognis-controller/0 (+https://github.com/fpytloun/cognis)"


class SearxngBackend:
    """Talks to a SearXNG instance over its JSON API."""

    def __init__(
        self,
        *,
        base_url: str,
        engines: str | None = None,
        categories: str | None = None,
        language: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_engines = engines
        self.default_categories = categories
        self.default_language = language
        self._breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

    async def search(
        self,
        query: str,
        *,
        num_results: int = 8,
        options: dict[str, Any] | None = None,
    ) -> ToolResult:
        if not query:
            return ToolResult(output="No search query provided.", is_error=True)
        if not self.base_url:
            return ToolResult(
                output=(
                    "SearXNG selected but no instance URL is configured. "
                    "Set web.searxng_url in Settings > Web."
                ),
                is_error=True,
            )

        opts = dict(options or {})
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "safesearch": _coerce_safesearch(opts.get("safesearch", 1)),
        }
        engines = opts.get("engines") or opts.get("searxng_engines") or self.default_engines
        if isinstance(engines, str) and engines.strip():
            params["engines"] = engines.strip()
        categories = (
            opts.get("categories") or opts.get("searxng_categories") or self.default_categories
        )
        if isinstance(categories, str) and categories.strip():
            params["categories"] = categories.strip()
        language = opts.get("language") or opts.get("searxng_language") or self.default_language
        if isinstance(language, str) and language.strip():
            params["language"] = language.strip()
        time_range = opts.get("time_range")
        if isinstance(time_range, str) and time_range.strip():
            params["time_range"] = time_range.strip()
        page = opts.get("pageno") or opts.get("page")
        if isinstance(page, int) and page > 0:
            params["pageno"] = page

        try:
            payload = await self._breaker.call(lambda: self._request(params))
        except CircuitBreakerError:
            return ToolResult(
                output="SearXNG unavailable (circuit breaker open). Try again later.",
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(
                output=f"SearXNG request timed out after {_SEARXNG_TIMEOUT_SECONDS:.0f}s.",
                is_error=True,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = exc.response.reason_phrase or "Unknown"
            if status == 429:
                return ToolResult(
                    output=(
                        "SearXNG instance rate-limited the request. "
                        "Wait or raise web.rate_limit.searxng_qps in Settings > Web."
                    ),
                    is_error=True,
                )
            if status in (401, 403):
                return ToolResult(
                    output=(
                        f"SearXNG instance refused the request (HTTP {status}). "
                        "Check that the instance allows JSON output and that "
                        "web.searxng_url points at the public endpoint."
                    ),
                    is_error=True,
                )
            return ToolResult(
                output=f"SearXNG HTTP error {status}: {reason}",
                is_error=True,
            )
        except httpx.RequestError as exc:
            logger.warning("web: SearXNG request failed: %s (%s)", type(exc).__name__, exc)
            return ToolResult(
                output=(
                    "SearXNG request failed (network error). "
                    "Check that web.searxng_url is reachable."
                ),
                is_error=True,
            )

        results = self._format_results(payload, num_results=num_results)
        if not results:
            return ToolResult(output="No search results found.")
        answer = self._extract_answer(payload)
        # Cast to the loose dict shape build_search_tool_result accepts.
        return build_search_tool_result(answer=answer, results=list(results))

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=_SEARXNG_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        ) as client:
            response = await client.get(f"{self.base_url}/search", params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise httpx.HTTPStatusError(
                    "SearXNG returned a non-object response.",
                    request=response.request,
                    response=response,
                )
            return data

    @staticmethod
    def _format_results(payload: dict[str, Any], *, num_results: int) -> list[dict[str, object]]:
        raw = payload.get("results")
        if not isinstance(raw, list):
            return []
        formatted: list[dict[str, object]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            snippet = str(entry.get("content") or entry.get("body") or "").strip()
            formatted.append({"title": title, "url": url, "snippet": snippet})
            if len(formatted) >= max(1, num_results):
                break
        return formatted

    @staticmethod
    def _extract_answer(payload: dict[str, Any]) -> str | None:
        answers = payload.get("answers")
        if isinstance(answers, list) and answers:
            first = answers[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                text = first.get("answer") or first.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
        infoboxes = payload.get("infoboxes")
        if isinstance(infoboxes, list) and infoboxes:
            box = infoboxes[0]
            if isinstance(box, dict):
                content = box.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        return None


def _coerce_safesearch(value: Any) -> int:
    """Coerce a safesearch option to SearXNG's 0/1/2 enum."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1, 2):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"off", "none", "0"}:
            return 0
        if normalized in {"moderate", "1", "default"}:
            return 1
        if normalized in {"strict", "2", "high"}:
            return 2
    return 1
