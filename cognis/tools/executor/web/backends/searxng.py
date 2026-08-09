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
import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.web.backends.formatting import build_search_tool_result
from cognis.tools.executor.web.backends.search_intent import (
    intent_metadata,
    result_type,
    search_mode,
)

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
        requested_mode = search_mode(opts)
        preferred_type = result_type(opts)
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "safesearch": _coerce_safesearch(opts.get("safesearch", 1)),
        }
        engines = opts.get("engines") or opts.get("searxng_engines") or self.default_engines
        if isinstance(engines, str) and engines.strip():
            params["engines"] = engines.strip()
        configured_categories = (
            opts.get("categories") or opts.get("searxng_categories") or self.default_categories
        )
        automatic_category = (
            None
            if engines or configured_categories
            else _automatic_category(requested_mode, preferred_type)
        )
        categories = configured_categories or automatic_category
        if isinstance(categories, str) and categories.strip():
            params["categories"] = categories.strip()
        language = opts.get("language") or opts.get("searxng_language") or self.default_language
        if isinstance(language, str) and language.strip():
            params["language"] = language.strip()
        time_range = opts.get("time_range") or opts.get("freshness")
        if isinstance(time_range, str) and time_range.strip():
            params["time_range"] = _normalize_time_range(time_range)
        page = opts.get("pageno") or opts.get("page")
        if isinstance(page, int) and page > 0:
            params["pageno"] = page

        try:
            payload = await self._breaker.call(
                lambda: self._request_without_counting_client_errors(params)
            )
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
        if isinstance(payload, ToolResult):
            return payload

        results = self._format_results(
            payload,
            query=query,
            num_results=num_results,
            options=opts,
        )
        initial_payload = payload
        initial_results = results
        effective_params = params
        category_fallback_attempted = False
        category_fallback_used = False
        category_fallback_failure: str | None = None
        category_initial_engine_failures = _engine_failures(initial_payload)
        category_fallback_engine_failures: list[object] = []
        if not results and automatic_category:
            category_fallback_attempted = True
            fallback_params = {key: value for key, value in params.items() if key != "categories"}
            try:
                fallback_payload = await self._breaker.call(
                    lambda: self._request_without_counting_client_errors(fallback_params)
                )
            except CircuitBreakerError:
                category_fallback_failure = "circuit breaker open"
            except httpx.HTTPStatusError as exc:
                category_fallback_failure = f"HTTP {exc.response.status_code}"
            except httpx.RequestError as exc:
                category_fallback_failure = type(exc).__name__
            else:
                if isinstance(fallback_payload, ToolResult):
                    retry_metadata = fallback_payload.metadata or {}
                    retry_status = retry_metadata.get("http_status")
                    retry_category = retry_metadata.get("failure_category")
                    category_fallback_failure = (
                        f"HTTP {retry_status}"
                        if retry_status
                        else str(retry_category or "backend error")
                    )
                elif isinstance(fallback_payload, dict):
                    fallback_results = self._format_results(
                        fallback_payload,
                        query=query,
                        num_results=num_results,
                        options=opts,
                    )
                    payload = fallback_payload
                    results = fallback_results
                    effective_params = fallback_params
                    category_fallback_used = True
                    category_fallback_engine_failures = _engine_failures(fallback_payload)
                    if not fallback_results:
                        category_fallback_failure = "no usable general results"

        requested_time_range = params.get("time_range")
        freshness_relaxation_attempted = False
        freshness_relaxed = False
        freshness_relaxation_failure: str | None = None
        freshness_relaxation_engine_failures: list[object] = []
        if not results and requested_time_range and not _engine_failures(payload):
            freshness_relaxation_attempted = True
            relaxed_params = {
                key: value for key, value in effective_params.items() if key != "time_range"
            }
            try:
                relaxed_payload = await self._breaker.call(
                    lambda: self._request_without_counting_client_errors(relaxed_params)
                )
            except CircuitBreakerError:
                freshness_relaxation_failure = "circuit breaker open"
                logger.warning(
                    "web: SearXNG freshness-relaxed retry rejected by circuit breaker",
                )
            except httpx.HTTPStatusError as exc:
                freshness_relaxation_failure = f"HTTP {exc.response.status_code}"
                logger.warning(
                    "web: SearXNG freshness-relaxed retry failed with HTTP %s",
                    exc.response.status_code,
                )
            except httpx.RequestError as exc:
                freshness_relaxation_failure = type(exc).__name__
                logger.warning(
                    "web: SearXNG freshness-relaxed retry failed: %s",
                    type(exc).__name__,
                )
            else:
                if isinstance(relaxed_payload, ToolResult):
                    retry_metadata = relaxed_payload.metadata or {}
                    retry_status = retry_metadata.get("http_status")
                    retry_category = retry_metadata.get("failure_category")
                    freshness_relaxation_failure = (
                        f"HTTP {retry_status}"
                        if retry_status
                        else str(retry_category or "backend error")
                    )
                elif isinstance(relaxed_payload, dict):
                    relaxed_options = dict(opts)
                    relaxed_options.pop("time_range", None)
                    relaxed_options.pop("freshness", None)
                    relaxed_results = self._format_results(
                        relaxed_payload,
                        query=query,
                        num_results=num_results,
                        options=relaxed_options,
                    )
                    if relaxed_results:
                        payload = relaxed_payload
                        results = relaxed_results
                        effective_params = relaxed_params
                        freshness_relaxed = True
                    else:
                        freshness_relaxation_engine_failures = _engine_failures(relaxed_payload)
                        if freshness_relaxation_engine_failures:
                            failure_count = len(freshness_relaxation_engine_failures)
                            freshness_relaxation_failure = (
                                f"{failure_count} engine failure(s) during relaxed retry"
                            )
        if freshness_relaxed and category_fallback_used:
            category_fallback_failure = None

        diagnostics = _search_diagnostics(
            payload,
            params=effective_params,
            results=results,
        )
        diagnostics.update(
            {
                "requested_time_range": requested_time_range,
                "effective_time_range": effective_params.get("time_range"),
                "freshness_relaxation_attempted": freshness_relaxation_attempted,
                "freshness_relaxed": freshness_relaxed,
                "freshness_relaxation_failure": freshness_relaxation_failure,
                "initial_raw_result_count": len(initial_payload.get("results") or []),
                "initial_returned_result_count": len(initial_results),
                "requested_category": automatic_category,
                "effective_category": effective_params.get("categories"),
                "category_fallback_attempted": category_fallback_attempted,
                "category_fallback_used": category_fallback_used,
                "category_fallback_failure": category_fallback_failure,
            }
        )
        existing_failures = diagnostics.get("engine_failures")
        merged_failures = _merge_engine_failures(
            list(existing_failures) if isinstance(existing_failures, list) else [],
            category_initial_engine_failures,
            category_fallback_engine_failures,
            freshness_relaxation_engine_failures,
        )
        if merged_failures:
            diagnostics["engine_failures"] = merged_failures
        if freshness_relaxed:
            diagnostics.update(
                {
                    "search_quality": "degraded",
                    "search_degraded": True,
                    "degraded_reason": (
                        "The requested freshness filter returned no usable results, "
                        "so it was relaxed. Dates remain unverified unless supplied "
                        "by the source."
                    ),
                }
            )
        elif freshness_relaxation_failure:
            diagnostics.update(
                {
                    "search_quality": "degraded",
                    "search_degraded": True,
                    "degraded_reason": (
                        "The requested freshness filter returned no usable results, "
                        f"and the relaxed retry failed ({freshness_relaxation_failure})."
                    ),
                }
            )
        if category_fallback_used:
            fallback_reason = (
                f"The '{automatic_category}' category returned no usable results, "
                "so the search fell back to the instance's general engines."
            )
            if freshness_relaxed:
                fallback_reason = (
                    f"The '{automatic_category}' category returned no usable results. "
                    "The general fallback also required relaxing the requested freshness "
                    "filter."
                )
            diagnostics.update(
                {
                    "search_quality": "degraded",
                    "search_degraded": True,
                    "degraded_reason": fallback_reason,
                }
            )
        elif category_fallback_failure:
            diagnostics.update(
                {
                    "search_quality": "degraded",
                    "search_degraded": True,
                    "degraded_reason": (
                        f"The '{automatic_category}' category returned no usable results, "
                        f"and the general fallback failed ({category_fallback_failure})."
                    ),
                }
            )
        if not results:
            effective_mode = (
                "web" if category_fallback_used and requested_mode != "web" else requested_mode
            )
            diagnostics.update(
                intent_metadata(
                    requested_mode=requested_mode,
                    effective_mode=effective_mode,
                    preferred_result_type=preferred_type,
                    native_mode_support=True,
                )
            )
            if diagnostics["search_quality"] == "degraded":
                return ToolResult(
                    output=(
                        "No usable search results found. Search coverage was degraded; "
                        "inspect the search diagnostics."
                    ),
                    metadata=diagnostics,
                )
            return ToolResult(output="No search results found.", metadata=diagnostics)
        answer = self._extract_answer(payload)
        # Cast to the loose dict shape build_search_tool_result accepts.
        effective_mode = (
            "web" if category_fallback_used and requested_mode != "web" else requested_mode
        )
        existing_degraded = bool(diagnostics.get("search_degraded"))
        mode_metadata = intent_metadata(
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            preferred_result_type=preferred_type,
            native_mode_support=True,
        )
        diagnostics.update(mode_metadata)
        diagnostics["search_degraded"] = existing_degraded or bool(mode_metadata["search_degraded"])
        return build_search_tool_result(
            answer=answer,
            results=[] if effective_mode == "images" else list(results),
            images=(
                _format_image_results(payload, limit=num_results, options=opts)
                if effective_mode == "images"
                else None
            ),
            metadata={
                **diagnostics,
                "normalized_results": results,
            },
        )

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

    async def _request_without_counting_client_errors(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any] | ToolResult:
        try:
            return await self._request(params)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                category = (
                    "rate_limited"
                    if status == 429
                    else "access_denied"
                    if status in {401, 403}
                    else "http_client_error"
                )
                guidance = {
                    401: "Check SearXNG authentication or access policy.",
                    403: (
                        "The SearXNG instance refused the request. Check JSON output, "
                        "access policy, and reverse-proxy rules."
                    ),
                    429: "The SearXNG instance rate-limited the request. Retry later.",
                }.get(status, "Check the SearXNG request configuration.")
                return ToolResult(
                    output=f"SearXNG request failed with HTTP {status}. {guidance}",
                    is_error=True,
                    metadata={
                        "backend": "searxng",
                        "http_status": status,
                        "failure_category": category,
                    },
                )
            raise

    @staticmethod
    def _format_results(
        payload: dict[str, Any],
        *,
        query: str,
        num_results: int,
        options: dict[str, Any],
    ) -> list[dict[str, object]]:
        raw = payload.get("results")
        if not isinstance(raw, list):
            return []
        include_domains = _domain_set(options.get("include_domains"))
        exclude_domains = _domain_set(options.get("exclude_domains"))
        preferred_type = result_type(options)
        query_tokens = {
            token for token in re.findall(r"[a-z0-9]+", query.lower()) if len(token) > 2
        }
        ranked: list[tuple[float, int, dict[str, object]]] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
            if not url:
                continue
            normalized_url = _normalized_result_url(url)
            if normalized_url in seen:
                continue
            seen.add(normalized_url)
            parsed_url = urlparse(url)
            host = (parsed_url.hostname or "").lower().removeprefix("www.")
            if exclude_domains and _matches_domain(
                host,
                exclude_domains,
                path=parsed_url.path,
            ):
                continue
            if include_domains and not _matches_domain(
                host,
                include_domains,
                path=parsed_url.path,
            ):
                continue
            snippet = str(entry.get("content") or entry.get("body") or "").strip()
            source_score = entry.get("score")
            provider_score = float(source_score) if isinstance(source_score, int | float) else 0.0
            score = provider_score
            title_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
            overlap = len(query_tokens & title_tokens) / max(len(query_tokens), 1)
            score += overlap * 3.0 + 1.0 / (index + 1)
            if include_domains and _matches_domain(
                host,
                include_domains,
                path=parsed_url.path,
            ):
                score += 2.0
            normalized_type = _infer_result_type(entry, url=url, title=title)
            source_metadata = _source_metadata(entry)
            score += _result_type_boost(preferred_type, url, title)
            score += _structured_authority_boost(
                query=query,
                query_tokens=query_tokens,
                result_type=normalized_type,
                url=url,
                title=title,
                source_metadata=source_metadata,
            )
            if re.search(r"/(?:tag|tags|category|categories|topic|topics|jobs?)(?:/|$)", url, re.I):
                score -= 1.5
            published_date = entry.get("publishedDate") or entry.get("published_date")
            freshness = _freshness_label(published_date, options.get("time_range"))
            if freshness == "stale":
                score -= 2.0
            recommendation, reason = _fetch_recommendation(
                result_type=normalized_type,
                snippet=snippet,
                engines=entry.get("engines") or [entry.get("engine")],
                freshness=freshness,
                url=url,
            )
            formatted = {
                "title": title,
                "url": url,
                "snippet": snippet,
                "score": round(score, 4),
                "cognis_score": round(score, 4),
                "provider_score": provider_score,
                "published_date": published_date,
                "freshness": freshness,
                "engine": entry.get("engine"),
                "engines": entry.get("engines"),
                "category": entry.get("category"),
                "result_type": normalized_type,
                "language": entry.get("language"),
                "thumbnail": entry.get("thumbnail") or entry.get("img_src"),
                "fetchability": "unknown",
                "fetch_recommendation": recommendation,
                "recommendation_reason": reason,
                "source_positions": entry.get("positions"),
                "source_metadata": source_metadata,
            }
            ranked.append((score, index, formatted))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[: max(1, num_results)]]

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


def _normalize_time_range(value: str) -> str:
    aliases = {"day": "day", "week": "week", "month": "month", "year": "year"}
    return aliases.get(value.strip().lower(), value.strip().lower())


def _domain_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(domain).strip().lower().removeprefix("www.") for domain in value if str(domain).strip()
    }


def _matches_domain(host: str, domains: set[str], *, path: str = "") -> bool:
    return any(
        host == domain
        or host.endswith(f".{domain}")
        or (
            domain == "pubmed.ncbi.nlm.nih.gov"
            and host == "ncbi.nlm.nih.gov"
            and path.startswith("/pubmed/")
        )
        for domain in domains
    )


def _normalized_result_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",
            parsed.query,
            "",
        )
    )


def _result_type_boost(result_type: str | None, url: str, title: str) -> float:
    if result_type is None:
        return 0.0
    value = f"{url} {title}".lower()
    patterns = {
        "discussion": r"(?:/comments/|/questions/\d+|thread|discussion|forum)",
        "paper": (
            r"(?:arxiv\.org/(?:abs|pdf)/|pubmed\.ncbi|"
            r"ncbi\.nlm\.nih\.gov/pubmed/|doi\.org|paper)"
        ),
        "repository": r"(?:github\.com/[^/]+/[^/]+|gitlab\.com/[^/]+/[^/]+)",
        "document": r"(?:\.pdf(?:\?|$)|/documents?/|/publications?/)",
    }
    pattern = patterns.get(result_type)
    return 2.0 if pattern and re.search(pattern, value) else 0.0


def _automatic_category(search_mode: str, result_type: str | None) -> str | None:
    mode_categories = {
        "news": "news",
        "images": "images",
        "videos": "videos",
    }
    if search_mode in mode_categories:
        return mode_categories[search_mode]
    if result_type in {"repository", "discussion"}:
        return "it"
    if result_type == "paper":
        return "science"
    return None


def _search_diagnostics(
    payload: dict[str, Any],
    *,
    params: dict[str, Any],
    results: list[dict[str, object]],
) -> dict[str, object]:
    failures_raw = payload.get("unresponsive_engines")
    failures = failures_raw if isinstance(failures_raw, list) else []
    contributing: set[str] = set()
    for result in results:
        engine = result.get("engine")
        engines = result.get("engines")
        if engine:
            contributing.add(str(engine))
        if isinstance(engines, list):
            contributing.update(str(item) for item in engines if item)
    requested = [
        item.strip() for item in str(params.get("engines") or "").split(",") if item.strip()
    ]
    degraded = bool(failures)
    return {
        "backend": "searxng",
        "search_quality": "degraded" if degraded else "healthy",
        "search_degraded": degraded,
        "engines_requested": requested,
        "engines_contributing": sorted(contributing),
        "engine_failures": failures,
        "raw_result_count": len(payload.get("results") or []),
        "returned_result_count": len(results),
        "requested_time_range": params.get("time_range"),
        "effective_time_range": params.get("time_range"),
        "freshness_relaxed": False,
        "requested_language": params.get("language"),
        "suggestions": payload.get("suggestions") or [],
        "corrections": payload.get("corrections") or [],
    }


def _infer_result_type(entry: dict[str, Any], *, url: str, title: str) -> str:
    template = str(entry.get("template") or "").lower()
    category = str(entry.get("category") or "").lower()
    engine = str(entry.get("engine") or "").lower()
    value = f"{url} {title}".lower()
    if category == "images" or "image" in template:
        return "image"
    if (
        engine in {"github", "gitlab"}
        or any(marker in value for marker in ("github.com/", "gitlab.com/"))
        or "github" in template
    ):
        return "repository"
    if engine in {"pubmed", "arxiv", "crossref", "google scholar"} or re.search(
        r"(?:pubmed\.ncbi|ncbi\.nlm\.nih\.gov/pubmed/|arxiv\.org/(?:abs|pdf)/|doi\.org)",
        value,
    ):
        return "paper"
    if engine in {"youtube", "vimeo"} or re.search(
        r"(?:youtube\.com/watch|youtu\.be/|vimeo\.com/)",
        value,
    ):
        return "video"
    if engine in {"reddit", "stackexchange"} or re.search(
        r"(?:reddit\.com/.*/comments/|stackoverflow\.com/questions/)",
        value,
    ):
        return "thread"
    if ".pdf" in value or "document" in template:
        return "document"
    if category == "news" or "news" in template:
        return "article"
    return "web"


def _format_image_results(
    payload: dict[str, Any],
    *,
    limit: int,
    options: dict[str, Any],
) -> list[dict[str, object]]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    images: list[dict[str, object]] = []
    seen: set[str] = set()
    include_domains = _domain_set(options.get("include_domains"))
    exclude_domains = _domain_set(options.get("exclude_domains"))
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        image_url = entry.get("img_src") or entry.get("thumbnail")
        if not isinstance(image_url, str) or not image_url.strip():
            continue
        normalized_url = image_url.strip()
        if normalized_url in seen:
            continue
        source_page_url = entry.get("url")
        if not isinstance(source_page_url, str) or not source_page_url.strip():
            continue
        parsed_source = urlparse(source_page_url.strip())
        source_host = (parsed_source.hostname or "").lower().removeprefix("www.")
        if exclude_domains and _matches_domain(
            source_host,
            exclude_domains,
            path=parsed_source.path,
        ):
            continue
        if include_domains and not _matches_domain(
            source_host,
            include_domains,
            path=parsed_source.path,
        ):
            continue
        seen.add(normalized_url)
        image: dict[str, object] = {"url": normalized_url}
        image["source_page_url"] = source_page_url.strip()
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            image["alt"] = title.strip()
        caption = entry.get("content")
        if isinstance(caption, str) and caption.strip():
            image["caption"] = caption.strip()
        engine = entry.get("engine")
        if isinstance(engine, str) and engine.strip():
            image["source"] = engine.strip()
        images.append(image)
        if len(images) >= max(1, limit):
            break
    return images


def _merge_engine_failures(*groups: list[object]) -> list[object]:
    merged: list[object] = []
    for group in groups:
        for failure in group:
            if failure not in merged:
                merged.append(failure)
    return merged


def _freshness_label(value: Any, requested: Any) -> str:
    if not value:
        return "unknown"
    text = str(value).strip()
    parsed: datetime | None = None
    with suppress(ValueError):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed is None:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - parsed.astimezone(UTC)).days
    limits = {"day": 2, "week": 8, "month": 32, "year": 370}
    limit = limits.get(str(requested or "").lower())
    if limit is None:
        return "known"
    return "stale" if limit is not None and age_days > limit else "current"


def _engine_failures(payload: dict[str, Any]) -> list[object]:
    failures = payload.get("unresponsive_engines")
    return failures if isinstance(failures, list) else []


def _structured_authority_boost(
    *,
    query: str,
    query_tokens: set[str],
    result_type: str,
    url: str,
    title: str,
    source_metadata: dict[str, object],
) -> float:
    """Prefer canonical structured sources over title-matching mirrors."""
    normalized_query = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
    value = f"{url} {title}".lower()
    identifier_tokens = re.findall(
        r"\b(?:10\.\d{4,9}/\S+|\d{7,9}|[a-z-]+\d{4}\.\d{4,5})\b",
        query.lower(),
    )
    if identifier_tokens and any(token.rstrip(".,)") in value for token in identifier_tokens):
        return 4.0

    if result_type == "video":
        author = str(source_metadata.get("author") or "").strip().lower()
        normalized_author = " ".join(re.findall(r"[a-z0-9]+", author))
        if normalized_author and normalized_author in normalized_query:
            return 5.0

    if result_type == "repository":
        path_parts = [part for part in urlparse(url).path.lower().split("/") if part]
        if len(path_parts) >= 2:
            owner_repo = set(re.findall(r"[a-z0-9]+", " ".join(path_parts[:2])))
            if owner_repo and owner_repo <= query_tokens:
                return 3.0

    return 0.0


def _fetch_recommendation(
    *,
    result_type: str,
    snippet: str,
    engines: Any,
    freshness: str,
    url: str,
) -> tuple[str, str]:
    engine_names = {str(item) for item in engines or [] if item}
    if freshness == "stale":
        return "low", "Result is older than the requested freshness window."
    if re.search(r"/(?:jobs?|tags?|categories?|topics?)(?:/|$)", url, re.I):
        return "low", "URL appears to be a listing or taxonomy page."
    specialized = engine_names & {"github", "pubmed", "arxiv", "youtube", "reddit"}
    if specialized and result_type != "web":
        return "high", f"Structured {result_type} result from {sorted(specialized)[0]}."
    if len(snippet) >= 120:
        return "medium", "Substantive snippet; fetch for complete source context."
    return "low", "Sparse metadata; verify relevance before fetching."


def _source_metadata(entry: dict[str, Any]) -> dict[str, object]:
    keys = (
        "author",
        "maintainer",
        "license",
        "popularity",
        "tags",
        "duration",
        "views",
        "iframe_src",
        "homepage",
        "repository_url",
    )
    nested = entry.get("metadata")
    allowed_nested = {
        "author",
        "maintainer",
        "license",
        "popularity",
        "tags",
        "duration",
        "views",
        "homepage",
    }
    result: dict[str, object] = {}
    if isinstance(nested, dict):
        for key in allowed_nested:
            value = _bounded_metadata_value(nested.get(key))
            if value not in (None, "", [], {}):
                result[key] = value
    result.update(
        {
            key: value
            for key in keys
            if key in entry
            and (value := _bounded_metadata_value(entry[key])) not in (None, "", [], {})
        }
    )
    return result


def _bounded_metadata_value(value: Any) -> object | None:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        return [str(item)[:100] for item in value[:30]]
    return None
