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
import multiprocessing
import queue
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import httpx

from cognis.models.tool import ToolResult
from cognis.providers.circuit_breaker import CircuitBreaker, CircuitBreakerError
from cognis.tools.executor.web.backends.formatting import build_search_tool_result
from cognis.tools.executor.web.backends.reddit import fetch_reddit_thread
from cognis.tools.executor.web.backends.search_intent import (
    domain_allowed,
    intent_metadata,
    result_type,
    search_mode,
    semantic_score,
)
from cognis.tools.executor.web.headers import (
    clamp_timeout,
    fetch_with_retry,
    format_response_result,
)
from cognis.tools.executor.web.public_adapters import dispatch_public_adapter

logger = logging.getLogger(__name__)
_DDG_REQUEST_TIMEOUT_SECONDS = 15

_MAX_ORIGIN_BREAKERS = 256
_fetch_breakers: OrderedDict[str, CircuitBreaker] = OrderedDict()
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

        public_result = await dispatch_public_adapter(
            url,
            timeout=timeout,
            output_format=output_format,
            options=options,
        )
        if public_result is not None:
            return public_result

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
            result = await _origin_breaker(url).call(
                lambda: _fetch_without_counting_client_errors(url, timeout=timeout)
            )
        except CircuitBreakerError:
            origin = _normalized_origin(url)
            return ToolResult(
                output=f"Web fetch temporarily unavailable for {origin} (circuit breaker open).",
                is_error=True,
                metadata={
                    "failure_category": "circuit_open",
                    "circuit_origin": origin,
                },
            )
        except httpx.TimeoutException as exc:
            return _request_error_result(exc, url=url, timeout=timeout)
        except httpx.HTTPStatusError as exc:
            return _http_status_result(exc)
        except httpx.RequestError as exc:
            return _request_error_result(exc, url=url, timeout=timeout)

        if isinstance(result, ToolResult):
            return result

        # result is an httpx.Response
        response_url = str(getattr(result, "url", "") or "")
        final_url = response_url if response_url.startswith(("http://", "https://")) else url
        if _looks_like_pdf_response(result):
            return await _format_pdf_response_in_process(
                result,
                output_format=output_format,
                requested_url=url,
                source_url=final_url,
                options=options,
                timeout=min(float(timeout), 30.0),
            )
        from cognis.tools.executor.web.extraction_process import format_response_in_process

        try:
            return await format_response_in_process(
                result,
                output_format=output_format,
                requested_url=url,
                source_url=final_url,
                options=options or {},
                timeout=min(float(timeout), 30.0),
            )
        except TimeoutError:
            return ToolResult(
                output="Web page extraction timed out.",
                is_error=True,
                metadata={"failure_category": "extraction_timeout"},
            )
        except RuntimeError:
            return ToolResult(
                output="Web page extraction failed.",
                is_error=True,
                metadata={"failure_category": "extraction_failed"},
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
        requested_mode = search_mode(opts)
        preferred_type = result_type(opts)
        region = opts.get("region", "us-en")
        safesearch = opts.get("safesearch", "moderate")
        requested_time_range = str(opts.get("time_range") or "any").lower()
        timelimit = opts.get("timelimit") or {
            "day": "d",
            "week": "w",
            "month": "m",
            "year": "y",
        }.get(requested_time_range)
        include_images = opts.get("include_images") is True
        image_limit = _clamp_image_limit(opts.get("image_limit"), default=10)

        try:
            result = await _search_breaker.call(
                lambda: _ddg_search(
                    query,
                    max_results=num_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                    include_images=include_images,
                    mode=requested_mode,
                    options=opts,
                    preferred_type=preferred_type,
                    image_limit=image_limit,
                )
            )
            metadata = dict(result.metadata or {})
            existing_degraded = bool(metadata.get("search_degraded"))
            mode_metadata = intent_metadata(
                requested_mode=requested_mode,
                effective_mode=requested_mode,
                preferred_result_type=preferred_type,
                native_mode_support=True,
            )
            metadata.update(mode_metadata)
            metadata.update(
                {
                    "backend": "direct",
                    "provider": "duckduckgo",
                    "requested_time_range": requested_time_range,
                    "search_degraded": (
                        existing_degraded or bool(mode_metadata["search_degraded"])
                    ),
                }
            )
            result.metadata = metadata
            return result
        except CircuitBreakerError:
            return ToolResult(
                output="Web search unavailable (circuit breaker open). Try again later.",
                is_error=True,
                metadata={
                    "backend": "direct",
                    "provider": "duckduckgo",
                    "failure_category": "circuit_open",
                },
            )
        except Exception as exc:
            category = _ddg_failure_category(exc)
            logger.warning(
                "web: DuckDuckGo search failed: %s (%s)",
                type(exc).__name__,
                category,
            )
            return ToolResult(
                output=f"DuckDuckGo search failed ({category.replace('_', ' ')}).",
                is_error=True,
                metadata={
                    "backend": "direct",
                    "provider": "duckduckgo",
                    "failure_category": category,
                    "exception_type": type(exc).__name__,
                },
            )


def _ddg_failure_category(exc: Exception) -> str:
    """Classify DDG client failures without depending on private exception types."""
    text = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if any(token in text for token in ("429", "ratelimit", "rate limit", "too many requests")):
        return "rate_limited"
    if any(token in text for token in ("403", "blocked", "captcha", "challenge")):
        return "blocked"
    if any(
        token in text
        for token in ("connect", "connection", "network", "dns", "temporarily unavailable")
    ):
        return "network"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "invalid_response"
    return "unexpected"


def _looks_like_pdf_response(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    content = response.content
    return (
        content_type == "application/pdf"
        or (isinstance(content, bytes | bytearray) and content.startswith(b"%PDF-"))
        or urlparse(str(response.url)).path.lower().endswith(".pdf")
    )


def _pdf_format_worker(
    result_queue: Any,
    status_code: int,
    headers: dict[str, str],
    content: bytes,
    url: str,
    output_format: str,
    requested_url: str,
    source_url: str,
    options: dict[str, Any] | None,
) -> None:
    try:
        response = httpx.Response(
            status_code,
            headers=headers,
            content=content,
            request=httpx.Request("GET", url),
        )
        result_queue.put(
            (
                "ok",
                format_response_result(
                    response,
                    output_format,
                    requested_url=requested_url,
                    source_url=source_url,
                    options=options,
                ),
            )
        )
    except BaseException as exc:  # pragma: no cover - process safety boundary
        result_queue.put(("error", type(exc).__name__))


async def _format_pdf_response_in_process(
    response: httpx.Response,
    *,
    output_format: str,
    requested_url: str,
    source_url: str,
    options: dict[str, Any] | None,
    timeout: float,
) -> ToolResult:
    """Parse untrusted PDFs in a process that can be forcibly terminated."""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_pdf_format_worker,
        args=(
            result_queue,
            response.status_code,
            dict(response.headers),
            response.content,
            str(response.url),
            output_format,
            requested_url,
            source_url,
            options,
        ),
        daemon=True,
    )
    process.start()
    terminated = False
    try:
        try:
            status, payload = await asyncio.to_thread(result_queue.get, True, timeout)
        except queue.Empty:
            process.terminate()
            terminated = True
            await asyncio.to_thread(process.join, 2.0)
            return ToolResult(
                output=f"PDF extraction exceeded its {timeout:g}s safety budget.",
                is_error=True,
                metadata={"failure_category": "pdf_extraction_timeout"},
            )
        await asyncio.to_thread(process.join, 2.0)
        if status == "ok" and isinstance(payload, ToolResult):
            return payload
        return ToolResult(
            output=f"PDF extraction failed ({payload}).",
            is_error=True,
            metadata={"failure_category": "pdf_extraction_failed"},
        )
    except asyncio.CancelledError:
        if process.is_alive():
            process.terminate()
            terminated = True
            await asyncio.to_thread(process.join, 2.0)
        raise
    finally:
        result_queue.close()
        if not terminated and process.is_alive():
            process.terminate()
            process.join(timeout=2.0)


def _normalized_origin(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


def _origin_breaker(url: str) -> CircuitBreaker:
    origin = _normalized_origin(url)
    breaker = _fetch_breakers.get(origin)
    if breaker is None:
        breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            should_trip=lambda exc: (
                _request_failure_category(exc)
                not in {"dns_resolution_failed", "tls_certificate_invalid"}
            ),
        )
        _fetch_breakers[origin] = breaker
        if len(_fetch_breakers) > _MAX_ORIGIN_BREAKERS:
            _fetch_breakers.popitem(last=False)
    else:
        _fetch_breakers.move_to_end(origin)
    return breaker


async def _fetch_without_counting_client_errors(
    url: str,
    *,
    timeout: int,
) -> httpx.Response | ToolResult:
    try:
        return await fetch_with_retry(url, timeout=timeout)
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            return _http_status_result(exc)
        raise


def _http_status_result(exc: httpx.HTTPStatusError) -> ToolResult:
    status = exc.response.status_code
    reason = exc.response.reason_phrase or "Unknown"
    metadata: dict[str, object] = {
        "http_status": status,
        "failure_category": "http_client_error" if status < 500 else "http_server_error",
    }
    if status in {401, 403, 429}:
        metadata["direct_fetch_blocked"] = True
    return ToolResult(
        output=f"HTTP {status}: {reason}",
        is_error=True,
        metadata=metadata,
    )


def _request_error_result(
    exc: httpx.RequestError,
    *,
    url: str,
    timeout: int,
) -> ToolResult:
    host = (urlparse(url).hostname or "requested host").lower()
    category = _request_failure_category(exc)
    if category == "dns_resolution_failed":
        output = f"Could not resolve host {host}; DNS returned no usable address."
        retry_with_browser = False
    elif category == "tls_certificate_invalid":
        output = (
            f"TLS certificate validation failed for {host}; refusing an insecure HTTP downgrade."
        )
        retry_with_browser = False
    elif category == "connection_timeout":
        output = f"Connection to {host} timed out after {timeout}s."
        retry_with_browser = True
    elif category == "read_timeout":
        output = f"Reading from {host} timed out after {timeout}s."
        retry_with_browser = True
    elif category == "request_timeout":
        output = f"Request to {host} timed out after {timeout}s."
        retry_with_browser = True
    else:
        output = f"Network connection to {host} failed."
        retry_with_browser = True
    return ToolResult(
        output=output,
        is_error=True,
        metadata={
            "failure_category": category,
            "failure_stage": "direct_transport",
            "requested_url": url,
            "browser_fallback_recommended": retry_with_browser,
        },
    )


def _request_failure_category(exc: BaseException) -> str:
    chain: list[str] = []
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        chain.append(str(current).lower())
        current = current.__cause__ or current.__context__
    detail = " ".join(chain)

    if any(
        marker in detail
        for marker in (
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname provided",
            "no address associated with hostname",
        )
    ):
        return "dns_resolution_failed"
    elif any(
        marker in detail
        for marker in (
            "certificate_verify_failed",
            "certificate verify failed",
            "hostname mismatch",
            "certificate is not valid for",
            "no alternative certificate subject name",
        )
    ):
        return "tls_certificate_invalid"
    elif isinstance(exc, httpx.ConnectTimeout):
        return "connection_timeout"
    elif isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    elif isinstance(exc, httpx.TimeoutException):
        return "request_timeout"
    return "network_error"


async def _ddg_search(
    query: str,
    *,
    max_results: int = 8,
    region: str = "us-en",
    safesearch: str = "moderate",
    timelimit: str | None = None,
    include_images: bool = False,
    mode: str = "web",
    options: dict[str, Any] | None = None,
    preferred_type: str | None = None,
    image_limit: int = 10,
) -> ToolResult:
    """Execute DuckDuckGo search in a thread (the library is sync)."""
    import asyncio

    def _sync_search() -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
        from ddgs import DDGS

        ddgs = DDGS(timeout=_DDG_REQUEST_TIMEOUT_SECONDS)
        text_results: list[dict[str, Any]] = []
        if mode == "web":
            text_results = list(
                ddgs.text(
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
            )
        elif mode == "news":
            text_results = list(
                ddgs.news(
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch=safesearch,
                    timelimit=timelimit,
                )
            )
        elif mode == "videos":
            text_results = list(
                ddgs.videos(
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch=safesearch,
                )
            )
        image_results: list[dict[str, object]] = []
        if include_images or mode == "images":
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
        return build_search_tool_result(
            answer=None,
            results=[],
            metadata={
                "requested_time_range": str((options or {}).get("time_range") or "any").lower()
            },
        )

    opts = options or {}
    formatted_results: list[dict[str, object]] = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "snippet": r.get("body", r.get("snippet", "")),
            "published_date": r.get("date") if mode == "news" else None,
            "result_type": "article" if mode == "news" else "video" if mode == "videos" else None,
            "source_metadata": {
                "author": r.get("publisher") or r.get("uploader"),
                "duration": r.get("duration"),
            }
            if mode == "videos"
            else {},
            "cognis_score": semantic_score(
                preferred_type,
                str(r.get("href", r.get("link", ""))),
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
        and domain_allowed(str(image.get("url") or ""), opts)
    ]
    return build_search_tool_result(
        answer=None,
        results=formatted_results,
        images=images,
        metadata={"requested_time_range": str(opts.get("time_range") or "any").lower()},
    )


def _clamp_image_limit(value: object, *, default: int) -> int:
    if not isinstance(value, int | float | str):
        return default
    try:
        return min(max(int(value), 0), 50)
    except (TypeError, ValueError):
        return default
