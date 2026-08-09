"""Brave Search web backend.

Uses the Brave Web Search API via raw httpx calls.
API docs: https://api-dashboard.search.brave.com/app/documentation/web-search

Brave is search-only — it has no URL fetch/extract endpoint.
When used as the default backend, web_fetch falls back to direct.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

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

_MODE_ENDPOINTS = {
    "web": "https://api.search.brave.com/res/v1/web/search",
    "news": "https://api.search.brave.com/res/v1/news/search",
    "images": "https://api.search.brave.com/res/v1/images/search",
    "videos": "https://api.search.brave.com/res/v1/videos/search",
}


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

_SITE_OPERATOR_RE = re.compile(
    r"(?<!\S)site:(?:https?://)?(?P<host>[^/\s]+)(?P<path>/[^\s]*)?",
    re.IGNORECASE,
)
_COUNTRY_LANGUAGE_FALLBACKS = {"CZ": ("cs", None)}


def _normalize_brave_query(query: str) -> str:
    """Keep Brave's site operator domain-only and retain useful path terms."""

    def replace(match: re.Match[str]) -> str:
        host = match.group("host")
        path = match.group("path") or ""
        terms = [
            token
            for token in re.findall(r"[A-Za-z0-9]+", path)
            if len(token) >= 3 and token.lower() not in {"html", "index"}
        ]
        suffix = " " + " ".join(terms) if terms else ""
        return f"site:{host}{suffix}"

    return re.sub(r"\s+", " ", _SITE_OPERATOR_RE.sub(replace, query)).strip()


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
        requested_mode = search_mode(opts)
        preferred_type = result_type(opts)
        effective_query = _provider_query(_normalize_brave_query(query), opts)
        requested_time_range = str(opts.get("time_range") or "any").lower()
        params: dict[str, Any] = {
            "q": effective_query,
            "count": min(num_results, 20),
        }
        request_metadata: dict[str, object] = {
            "original_query": query,
            "effective_query": effective_query,
            "query_normalized": effective_query != query,
        }

        # Map all supported Brave search parameters
        country = opts.get("country")
        locale_fallback: tuple[str, str | None] | None = None
        if isinstance(country, str) and country.strip():
            requested_country = country.strip().upper()
            locale_fallback = _COUNTRY_LANGUAGE_FALLBACKS.get(requested_country)
            if locale_fallback:
                params.setdefault("search_lang", locale_fallback[0])
                if locale_fallback[1]:
                    params.setdefault("ui_lang", locale_fallback[1])
                request_metadata.update(
                    {
                        "search_degraded": True,
                        "degraded_reason": (
                            f"Brave does not support country '{requested_country}'; "
                            "used language targeting instead."
                        ),
                        "country_requested": requested_country,
                        "country_effective": None,
                        "locale_fallback": True,
                        "search_language_effective": locale_fallback[0],
                        "ui_language_effective": locale_fallback[1],
                    }
                )
            else:
                params["country"] = requested_country
        _set_if(params, opts, "search_lang")
        _set_if(params, opts, "ui_lang")
        if locale_fallback:
            request_metadata["search_language_effective"] = params.get("search_lang")
            request_metadata["ui_language_effective"] = params.get("ui_lang")
        _set_if(params, opts, "offset")
        if "safesearch" in opts:
            safesearch = opts["safesearch"]
            # Brave Images accepts only "strict" or "off"; map the shared
            # contract's "moderate" default to its safe native equivalent.
            params["safesearch"] = (
                "strict" if requested_mode == "images" and safesearch == "moderate" else safesearch
            )
        _set_if(params, opts, "freshness")  # pd, pw, pm, py, or date range
        if "freshness" not in params and requested_time_range in {"day", "week", "month", "year"}:
            params["freshness"] = {
                "day": "pd",
                "week": "pw",
                "month": "pm",
                "year": "py",
            }[requested_time_range]
        _set_if(params, opts, "text_decorations")
        _set_if(params, opts, "spellcheck")
        _set_if(params, opts, "extra_snippets")
        _set_if(params, opts, "goggles_id")
        _set_if(params, opts, "units")
        _set_if(params, opts, "result_filter")
        effective_params = params

        try:
            result = await _breaker.call(lambda: self._get(_MODE_ENDPOINTS[requested_mode], params))
        except httpx.HTTPStatusError as exc:
            requested_country = params.get("country")
            if (
                exc.response.status_code == 422
                and isinstance(requested_country, str)
                and requested_country != "ALL"
                and _brave_error_mentions_country(exc.response)
            ):
                retry_params = dict(params)
                retry_params["country"] = "ALL"
                try:
                    result = await _breaker.call(
                        lambda: self._get(_MODE_ENDPOINTS[requested_mode], retry_params)
                    )
                except CircuitBreakerError:
                    return ToolResult(
                        output="Brave Search unavailable (circuit breaker open). Try again later.",
                        is_error=True,
                    )
                except httpx.TimeoutException:
                    return ToolResult(output="Brave Search request timed out.", is_error=True)
                except httpx.HTTPStatusError as retry_exc:
                    return _brave_http_error_result(retry_exc)
                except httpx.RequestError:
                    return ToolResult(output="Brave Search request failed.", is_error=True)
                request_metadata.update(
                    {
                        "search_degraded": True,
                        "degraded_reason": (
                            f"Brave rejected country code '{requested_country}'; "
                            "retried with global country targeting."
                        ),
                        "country_requested": requested_country,
                        "country_effective": "ALL",
                        "country_filter_applied": False,
                    }
                )
                effective_params = retry_params
            else:
                return _brave_http_error_result(exc)
        except CircuitBreakerError:
            return ToolResult(
                output="Brave Search unavailable (circuit breaker open). Try again later.",
                is_error=True,
            )
        except httpx.TimeoutException:
            return ToolResult(output="Brave Search request timed out.", is_error=True)
        except httpx.RequestError:
            return ToolResult(output="Brave Search request failed.", is_error=True)

        formatted = _format_brave_results(
            result,
            mode=requested_mode,
            preferred_type=preferred_type,
            query=query,
            options=opts,
            request_metadata=request_metadata,
        )
        if (
            requested_time_range in {"day", "week", "month", "year"}
            and not formatted.is_error
            and int((formatted.metadata or {}).get("returned_result_count") or 0) == 0
        ):
            retry_params = dict(effective_params)
            retry_params.pop("freshness", None)
            try:
                relaxed = await _breaker.call(
                    lambda: self._get(_MODE_ENDPOINTS[requested_mode], retry_params)
                )
            except (CircuitBreakerError, httpx.HTTPError):
                return formatted
            relaxed_metadata = {
                **request_metadata,
                "search_degraded": True,
                "degraded_reason": (
                    "The requested freshness filter returned no usable results, "
                    "so it was relaxed. Verify dates from fetched source pages."
                ),
                "freshness_relaxed": True,
                "requested_time_range": requested_time_range,
                "effective_time_range": None,
            }
            recovered = _format_brave_results(
                relaxed,
                mode=requested_mode,
                preferred_type=preferred_type,
                query=query,
                options={key: value for key, value in opts.items() if key != "time_range"},
                request_metadata=relaxed_metadata,
            )
            if int((recovered.metadata or {}).get("returned_result_count") or 0) > 0:
                return recovered
        return formatted

    async def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a GET request to Brave Search API.

        Raises on failure so the circuit breaker can count errors.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                endpoint,
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


def _provider_query(query: str, options: dict[str, Any]) -> str:
    include = sorted(_domain_set(options.get("include_domains")))[:3]
    exclude = sorted(_domain_set(options.get("exclude_domains")))[:5]
    if include and not _SITE_OPERATOR_RE.search(query):
        site_clause = (
            f"site:{include[0]}"
            if len(include) == 1
            else "(" + " OR ".join(f"site:{domain}" for domain in include) + ")"
        )
        query = f"{query} {site_clause}"
    for domain in exclude:
        query = f"{query} -site:{domain}"
    return re.sub(r"\s+", " ", query).strip()


def _brave_http_error_result(exc: httpx.HTTPStatusError) -> ToolResult:
    status = exc.response.status_code
    detail = _brave_error_detail(exc.response)
    suffix = f" — {detail}" if detail else ""
    if status == 429:
        return ToolResult(
            output=f"Brave Search rate limit exceeded. Try again later.{suffix}",
            is_error=True,
            metadata={"http_status": status},
        )
    reason = exc.response.reason_phrase or "Unknown"
    return ToolResult(
        output=f"Brave Search API error (HTTP {status}): {reason}{suffix}",
        is_error=True,
        metadata={"http_status": status},
    )


def _brave_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if isinstance(payload, dict):
        payload = payload.get("message") or payload.get("detail") or payload.get("error") or ""
    detail = re.sub(r"\s+", " ", str(payload)).strip()
    return detail[:300]


def _brave_error_mentions_country(response: httpx.Response) -> bool:
    return "country" in _brave_error_detail(response).lower()


def _format_brave_results(
    data: dict[str, Any],
    *,
    mode: str = "web",
    preferred_type: str | None = None,
    query: str = "",
    options: dict[str, Any] | None = None,
    request_metadata: dict[str, object] | None = None,
) -> ToolResult:
    """Format Brave Search API response into readable output."""
    results = (
        (data.get("web") or {}).get("results", []) if mode == "web" else data.get("results", [])
    )
    metadata = intent_metadata(
        requested_mode=mode,
        effective_mode=mode,
        preferred_result_type=preferred_type,
        native_mode_support=True,
    )
    metadata["requested_time_range"] = str((options or {}).get("time_range") or "any").lower()
    metadata.update(request_metadata or {})

    if not results:
        return build_search_tool_result(answer=None, results=[], metadata=metadata)

    if mode == "images":
        images: list[dict[str, object]] = []
        for row in results:
            properties = row.get("properties") or {}
            thumbnail = row.get("thumbnail") or {}
            image_url = properties.get("url") or thumbnail.get("src")
            if not isinstance(image_url, str) or not image_url:
                continue
            opts = options or {}
            if not _domain_allowed(
                str(row.get("url") or ""),
                _domain_set(opts.get("include_domains")),
                _domain_set(opts.get("exclude_domains")),
            ):
                continue
            images.append(
                {
                    "url": image_url,
                    "alt": row.get("title"),
                    "caption": row.get("title"),
                    "source": row.get("source") or "brave_images",
                    "source_page_url": row.get("url"),
                }
            )
        return build_search_tool_result(
            answer=None,
            results=[],
            images=images,
            metadata=metadata,
        )

    formatted_results: list[dict[str, object]] = []
    for provider_rank, r in enumerate(results, start=1):
        description = r.get("description", "")
        extra = r.get("extra_snippets", [])
        snippet_parts = [str(description)] if description else []
        if isinstance(extra, list):
            snippet_parts.extend(str(item) for item in extra[:3] if item)
        formatted: dict[str, object] = {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": " ".join(part for part in snippet_parts if part),
            "published_date": r.get("page_age") or r.get("age"),
        }
        if mode == "news":
            formatted["result_type"] = "article"
        elif mode == "videos":
            formatted["result_type"] = "video"
            video = r.get("video") or {}
            formatted["source_metadata"] = {
                "author": video.get("creator"),
                "duration": video.get("duration"),
                "publisher": video.get("publisher"),
            }
        score = _semantic_score(
            preferred_type,
            str(r.get("url") or ""),
            str(r.get("title") or ""),
            str(formatted.get("snippet") or ""),
            query=query,
            provider_rank=provider_rank,
        )
        if mode == "news":
            score += _news_quality_adjustment(formatted)
        formatted["cognis_score"] = score
        formatted["provider_rank"] = provider_rank
        formatted_results.append(formatted)

    opts = options or {}
    include_domains = _domain_set(opts.get("include_domains"))
    exclude_domains = _domain_set(opts.get("exclude_domains"))
    formatted_results = [
        row
        for row in formatted_results
        if _domain_allowed(str(row.get("url") or ""), include_domains, exclude_domains)
    ]
    formatted_results.sort(
        key=lambda row: (
            -float(row["cognis_score"]) if isinstance(row.get("cognis_score"), int | float) else 0.0
        )
    )
    duplicate_count = 0
    if mode == "news":
        deduped: list[dict[str, object]] = []
        seen_titles: set[str] = set()
        per_domain: dict[str, int] = {}
        for row in formatted_results:
            title_key = " ".join(re.findall(r"[a-z0-9]+", str(row.get("title") or "").lower()))
            host = (urlparse(str(row.get("url") or "")).hostname or "").removeprefix("www.")
            if title_key in seen_titles or per_domain.get(host, 0) >= 2:
                duplicate_count += 1
                continue
            seen_titles.add(title_key)
            per_domain[host] = per_domain.get(host, 0) + 1
            deduped.append(row)
        formatted_results = deduped
        metadata["deduplicated_result_count"] = duplicate_count
    metadata["normalized_results"] = formatted_results

    return build_search_tool_result(
        answer=None,
        results=formatted_results,
        metadata=metadata,
    )


def _news_quality_adjustment(row: dict[str, object]) -> float:
    url = str(row.get("url") or "").lower()
    title = str(row.get("title") or "").lower()
    snippet = str(row.get("snippet") or "").lower()
    score = 0.0
    if row.get("published_date"):
        score += 1.5
    if re.search(
        r"/(?:tag|category|topic|search)/|(?:^|\W)(?:tag|category) page", url + " " + title
    ):
        score -= 4.0
    if re.search(
        r"\b(?:press release|pr newswire|business wire|globenewswire)\b",
        f"{url} {title} {snippet}",
    ):
        score -= 3.5
    if len(snippet) < 80:
        score -= 1.0
    return score


def _semantic_score(
    preferred_type: str | None,
    url: str,
    title: str,
    snippet: str = "",
    *,
    query: str = "",
    provider_rank: int = 1,
) -> float:
    query_terms = {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) >= 2 and token not in {"and", "or", "not", "site", "http", "https", "www"}
    }
    title_terms = set(re.findall(r"[a-z0-9]+", title.lower()))
    snippet_terms = set(re.findall(r"[a-z0-9]+", snippet.lower()))
    url_terms = set(re.findall(r"[a-z0-9]+", url.lower()))
    score = 2.0 / max(provider_rank, 1)
    if query_terms:
        score += 6.0 * len(query_terms & title_terms) / len(query_terms)
        score += 2.5 * len(query_terms & snippet_terms) / len(query_terms)
        score += 1.5 * len(query_terms & url_terms) / len(query_terms)
    if not preferred_type:
        return round(score, 4)
    value = f"{url} {title}".lower()
    patterns = {
        "paper": r"(?:arxiv\.org|pubmed\.ncbi|doi\.org|research paper)",
        "repository": r"(?:github\.com|gitlab\.com|codeberg\.org)",
        "discussion": r"(?:reddit\.com|stackoverflow\.com|/forum|/discussion|/questions/)",
        "document": r"(?:\.pdf(?:\?|$)|/docs?/|/documentation/|/manuals?/)",
    }
    if re.search(patterns[preferred_type], value):
        score += 2.0
    return round(score, 4)


def _domain_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).lower().removeprefix("www.") for item in value if str(item)}


def _domain_allowed(url: str, include: set[str], exclude: set[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")

    def matches(domain: str) -> bool:
        return host == domain or host.endswith(f".{domain}")

    return not any(matches(domain) for domain in exclude) and (
        not include or any(matches(domain) for domain in include)
    )
