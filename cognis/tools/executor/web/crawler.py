"""Bounded async BFS crawler for ``web_crawl``.

Used when the configured fetch backend is *not* Tavily (Tavily has its
own crawl endpoint and is preferred when available). The crawler:

* runs a bounded breadth-first walk from the supplied root URL,
* clamps depth, breadth, and total page limits,
* defers all per-URL fetching to the resolved fetch backend so the
  Stage A-C stealth stack and W1 auto-fallback to the headless browser
  apply to every fetched page,
* delegates concurrency to the W1 :class:`WebConcurrencyController`
  so the per-host cap and per-backend qps limit are respected.

robots.txt is **not** consulted by default (Tavily's own crawler does
not respect it either, and the agent decision-loop is the right place
to make that call). Pass ``respect_robots=True`` per-call to opt in.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.backends.protocol import WebFetchBackend
from cognis.tools.executor.web.concurrency import (
    WebConcurrencyController,
    host_for,
)
from cognis.tools.executor.web.headers import BROWSER_HEADERS, sanitise_url

logger = logging.getLogger(__name__)

_HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_IGNORED_SCHEMES = ("javascript:", "mailto:", "tel:", "#")
_DEFAULT_PAGE_TIMEOUT = 30


async def crawl_site(
    *,
    url: str,
    fetch_backend: WebFetchBackend,
    backend_label: str,
    controller: WebConcurrencyController,
    options: dict[str, Any] | None = None,
) -> ToolResult:
    """BFS crawl from ``url`` using the configured fetch backend."""
    options = options or {}
    max_depth = _coerce_int(options.get("max_depth"), default=1, lo=1, hi=5)
    max_breadth = _coerce_int(options.get("max_breadth"), default=20, lo=1, hi=500)
    limit = _coerce_int(options.get("limit"), default=50, lo=1, hi=500)
    page_timeout = _coerce_int(options.get("timeout"), default=_DEFAULT_PAGE_TIMEOUT, lo=1, hi=120)
    output_format = str(options.get("format") or "markdown")
    if output_format not in {"text", "markdown", "html"}:
        output_format = "markdown"
    same_host_only = not bool(options.get("allow_external"))
    select_domains = _coerce_str_list(options.get("select_domains"))
    exclude_domains = _coerce_str_list(options.get("exclude_domains"))
    select_paths = _coerce_str_list(options.get("select_paths"))
    exclude_paths = _coerce_str_list(options.get("exclude_paths"))

    try:
        start_url = sanitise_url(url)
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    base_host = (urlparse(start_url).hostname or "").lower()
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(start_url, 0)]
    pages: list[dict[str, Any]] = []

    sem = asyncio.Semaphore(max(1, min(8, max_breadth)))

    async def _fetch_one(target: str) -> tuple[str, ToolResult, list[str]]:
        async with (
            sem,
            controller.acquire(backend=backend_label, host=host_for(target), op="fetch"),
        ):
            try:
                result = await fetch_backend.fetch(
                    target,
                    output_format=output_format,
                    timeout=page_timeout,
                )
            except Exception as exc:
                logger.debug("web: crawl fetch raised: %s", exc)
                return (
                    target,
                    ToolResult(
                        output=f"fetch failed: {type(exc).__name__}",
                        is_error=True,
                    ),
                    [],
                )
        # Discover links by reading the raw HTML (best effort).
        links: list[str] = []
        try:
            links = await _discover_links(target, page_timeout=page_timeout)
        except Exception as exc:
            logger.debug("web: link discovery failed: %s", exc)
        return target, result, links

    pages_processed = 0
    depth_now = 0
    while queue and pages_processed < limit and depth_now <= max_depth:
        # Per-batch budget — never schedule more fetches than ``limit`` will
        # actually accept, so we don't pay for fetches whose results are
        # discarded.
        budget = limit - pages_processed
        batch_cap = min(max_breadth, max(1, budget))
        batch: list[str] = []
        next_queue: list[tuple[str, int]] = []
        for target, depth in queue:
            if depth != depth_now:
                next_queue.append((target, depth))
                continue
            if target in visited:
                continue
            if not _matches_filters(
                target=target,
                base_host=base_host,
                same_host_only=same_host_only,
                select_domains=select_domains,
                exclude_domains=exclude_domains,
                select_paths=select_paths,
                exclude_paths=exclude_paths,
            ):
                continue
            visited.add(target)
            batch.append(target)
            if len(batch) >= batch_cap:
                # Defer everything else at this depth to the next loop iter.
                next_queue.extend(
                    [pair for pair in queue if pair[0] not in visited and pair[1] == depth_now]
                )
                break
        if not batch:
            depth_now += 1
            queue = next_queue
            continue

        results = await asyncio.gather(*[_fetch_one(t) for t in batch])
        for target, result, links in results:
            pages_processed += 1
            pages.append(
                {
                    "url": target,
                    "depth": depth_now,
                    "is_error": bool(result.is_error),
                    "content": result.output,
                }
            )
            if depth_now < max_depth and not result.is_error:
                for raw_link in links:
                    if raw_link in visited:
                        continue
                    if any(target == raw_link for target, _ in next_queue):
                        continue
                    next_queue.append((raw_link, depth_now + 1))
            if pages_processed >= limit:
                break

        # Advance depth, drop visited entries, deduplicate.
        depth_now += 1
        deduped: list[tuple[str, int]] = []
        seen_urls: set[str] = set()
        for url_, depth in next_queue:
            if url_ in visited or url_ in seen_urls:
                continue
            seen_urls.add(url_)
            deduped.append((url_, depth))
        queue = deduped

    return _format_crawl_result(start_url, pages)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, result))


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _matches_filters(
    *,
    target: str,
    base_host: str,
    same_host_only: bool,
    select_domains: list[str],
    exclude_domains: list[str],
    select_paths: list[str],
    exclude_paths: list[str],
) -> bool:
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower()
    if same_host_only and base_host and host != base_host:
        return False
    if select_domains and not any(
        host == d.lower() or host.endswith("." + d.lower()) for d in select_domains
    ):
        return False
    if exclude_domains and any(
        host == d.lower() or host.endswith("." + d.lower()) for d in exclude_domains
    ):
        return False
    path = parsed.path or "/"
    if select_paths and not any(re.search(pattern, path) for pattern in select_paths):
        return False
    return not (exclude_paths and any(re.search(pattern, path) for pattern in exclude_paths))


async def _discover_links(target: str, *, page_timeout: int) -> list[str]:
    """Best-effort raw-HTML link discovery for a fetched page.

    The crawler doesn't share the backend's already-converted output
    because formats other than HTML strip anchor tags; instead we issue
    a side fetch with the standard browser headers. This is bounded and
    deduplicated upstream so the cost is small.
    """
    async with httpx.AsyncClient(
        timeout=min(page_timeout, 30),
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        try:
            response = await client.get(target)
        except httpx.RequestError:
            return []
        if response.status_code >= 400:
            return []
        out: list[str] = []
        for raw in _HREF_RE.findall(response.text or ""):
            href = raw.strip()
            if not href:
                continue
            if href.startswith(_IGNORED_SCHEMES):
                continue
            out.append(urljoin(target, href))
            if len(out) >= 200:
                break
        return out


def _format_crawl_result(root_url: str, pages: list[dict[str, Any]]) -> ToolResult:
    if not pages:
        return ToolResult(output=f"No pages crawled from {root_url}.")
    chunks: list[str] = [f"# Crawl results for {root_url}", ""]
    chunks.append(f"Pages: {len(pages)}")
    chunks.append("")
    for page in pages:
        marker = "ERROR" if page["is_error"] else f"depth={page['depth']}"
        chunks.append(f"## {page['url']} ({marker})")
        body = page["content"]
        if isinstance(body, str) and body:
            # Per-page truncation so a huge crawl doesn't blow context budgets.
            chunks.append(body[:8000])
            if len(body) > 8000:
                chunks.append("\n[truncated]")
        chunks.append("")
    return ToolResult(
        output="\n".join(chunks),
        metadata={"crawl_pages": len(pages)},
    )
