"""Direct site mapping for ``web_map``.

The direct mapper mirrors Tavily's map shape as closely as practical without
using an external crawl service:

* discover sitemap URLs from ``robots.txt`` and common sitemap locations,
* recursively expand sitemap indexes, including static ``.gz`` sitemap files,
* apply URL filters while collecting page URLs,
* fall back to a bounded link-only BFS crawl when no usable sitemap URLs exist.
"""

from __future__ import annotations

import gzip
import io
import logging
import re
from collections import deque
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from cognis.tools.executor.web.headers import BROWSER_HEADERS, sanitise_url

logger = logging.getLogger(__name__)

_SITEMAP_TIMEOUT_SECONDS = 15.0
_DEFAULT_PAGE_TIMEOUT_SECONDS = 30.0
_MAX_SITEMAPS_TO_FETCH = 200
_MAX_SITEMAP_COMPRESSED_BYTES = 10 * 1024 * 1024
_MAX_SITEMAP_TEXT_BYTES = 10 * 1024 * 1024
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap\s*:\s*(\S+)\s*$")
_IGNORED_SCHEMES = ("javascript:", "mailto:", "tel:", "#")


async def map_site_urls(
    base_url: str,
    *,
    options: dict[str, Any] | None = None,
) -> tuple[list[str], str]:
    """Map URLs reachable from ``base_url`` using direct discovery.

    Returns ``(urls, source)`` where ``source`` is one of ``"sitemap"``,
    ``"sitemap_index"`` or ``"html_crawl"``.
    """
    opts = options or {}
    base_url = sanitise_url(base_url)
    base_host = (urlparse(base_url).hostname or "").lower()
    limit = _coerce_int(opts.get("limit"), default=200, lo=1, hi=500)
    max_depth = _coerce_int(opts.get("max_depth"), default=1, lo=1, hi=5)
    max_breadth = _coerce_int(opts.get("max_breadth"), default=20, lo=1, hi=500)
    timeout = _coerce_float(
        opts.get("timeout"),
        default=_DEFAULT_PAGE_TIMEOUT_SECONDS,
        lo=1.0,
        hi=120.0,
    )
    filters = _UrlFilters(
        base_host=base_host,
        same_host_only=not bool(opts.get("allow_external")),
        select_domains=_coerce_str_list(opts.get("select_domains")),
        exclude_domains=_coerce_str_list(opts.get("exclude_domains")),
        select_paths=_coerce_str_list(opts.get("select_paths")),
        exclude_paths=_coerce_str_list(opts.get("exclude_paths")),
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        sitemap_urls = await _candidate_sitemap_urls(client, base_url)
        urls, source = await _expand_sitemaps(
            client,
            sitemap_urls=sitemap_urls,
            filters=filters,
            limit=limit,
        )
        if urls:
            return urls, source

        crawled = await _crawl_html_links(
            client,
            start_url=base_url,
            filters=filters,
            limit=limit,
            max_depth=max_depth,
            max_breadth=max_breadth,
        )
        return crawled, "html_crawl"


async def discover_sitemap_urls(
    base_url: str,
    *,
    limit: int = 200,
    same_host_only: bool = True,
) -> tuple[list[str], str]:
    """Backward-compatible wrapper for direct ``web_map`` URL discovery."""
    return await map_site_urls(
        base_url,
        options={"limit": limit, "allow_external": not same_host_only},
    )


async def _candidate_sitemap_urls(client: httpx.AsyncClient, base_url: str) -> list[str]:
    candidates: list[str] = []

    robots_url = urljoin(base_url, "/robots.txt")
    try:
        response = await client.get(robots_url)
    except httpx.RequestError as exc:
        logger.debug("web: robots.txt fetch failed: %s", exc)
    else:
        if response.status_code < 400:
            for raw_url in _ROBOTS_SITEMAP_RE.findall(response.text or ""):
                _append_unique(candidates, urljoin(base_url, raw_url.strip()))

    _append_unique(candidates, urljoin(base_url, "/sitemap.xml"))
    _append_unique(candidates, urljoin(base_url, "/sitemap_index.xml"))
    return candidates


async def _expand_sitemaps(
    client: httpx.AsyncClient,
    *,
    sitemap_urls: list[str],
    filters: _UrlFilters,
    limit: int,
) -> tuple[list[str], str]:
    queue: deque[str] = deque(sitemap_urls)
    fetched: set[str] = set()
    page_urls: list[str] = []
    saw_sitemap_index = False

    while queue and len(page_urls) < limit and len(fetched) < _MAX_SITEMAPS_TO_FETCH:
        sitemap_url = queue.popleft()
        if sitemap_url in fetched:
            continue
        if not _matches_domain_filters(sitemap_url, filters):
            continue
        fetched.add(sitemap_url)

        try:
            response = await client.get(sitemap_url, timeout=_SITEMAP_TIMEOUT_SECONDS)
        except httpx.RequestError as exc:
            logger.debug("web: sitemap fetch failed for %s: %s", sitemap_url, exc)
            continue
        if response.status_code >= 400:
            continue

        text = _response_text(response, sitemap_url)
        if not text:
            continue

        locs = [urljoin(sitemap_url, loc.strip()) for loc in _LOC_RE.findall(text)]
        if not locs:
            continue

        if _looks_like_sitemap_index(text, locs):
            saw_sitemap_index = True
            for child_sitemap_url in locs:
                if child_sitemap_url not in fetched and _matches_domain_filters(
                    child_sitemap_url, filters
                ):
                    queue.append(child_sitemap_url)
            continue

        for page_url in locs:
            if not _matches_filters(page_url, filters):
                continue
            _append_unique(page_urls, page_url)
            if len(page_urls) >= limit:
                break

    if page_urls:
        return page_urls[:limit], "sitemap_index" if saw_sitemap_index else "sitemap"
    return [], "sitemap_index" if saw_sitemap_index else "sitemap"


async def _crawl_html_links(
    client: httpx.AsyncClient,
    *,
    start_url: str,
    filters: _UrlFilters,
    limit: int,
    max_depth: int,
    max_breadth: int,
) -> list[str]:
    seen: set[str] = set()
    queued: set[str] = {start_url}
    discovered: list[str] = []
    current_level: list[str] = [start_url]

    for depth in range(max_depth + 1):
        if not current_level or len(discovered) >= limit:
            break
        next_level: list[str] = []
        for target in current_level[:max_breadth]:
            queued.discard(target)
            if target in seen:
                continue
            if not _matches_filters(target, filters):
                continue
            seen.add(target)
            _append_unique(discovered, target)
            if len(discovered) >= limit:
                break
            if depth >= max_depth:
                continue

            try:
                response = await client.get(target)
            except httpx.RequestError as exc:
                logger.debug("web: html crawl fetch failed for %s: %s", target, exc)
                continue
            if response.status_code >= 400:
                continue

            for link in _extract_links(target, response.text or ""):
                if link in seen or link in queued:
                    continue
                if not _matches_filters(link, filters):
                    continue
                next_level.append(link)
                queued.add(link)
        current_level = next_level

    return discovered[:limit]


def _response_text(response: httpx.Response, url: str) -> str:
    content = response.content
    if urlparse(url).path.endswith(".gz"):
        if len(content) > _MAX_SITEMAP_COMPRESSED_BYTES:
            logger.debug("web: sitemap %s compressed payload exceeded size cap", url)
            return ""
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as gzip_file:
                content = gzip_file.read(_MAX_SITEMAP_TEXT_BYTES + 1)
        except OSError:
            logger.debug("web: sitemap %s was not gzip-compressed", url)
    if len(content) > _MAX_SITEMAP_TEXT_BYTES:
        logger.debug("web: sitemap %s text payload exceeded size cap", url)
        content = content[:_MAX_SITEMAP_TEXT_BYTES]
    try:
        return content.decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _looks_like_sitemap_index(text: str, locs: list[str]) -> bool:
    lowered = text[:1000].lower()
    if "<sitemapindex" in lowered:
        return True
    if "<urlset" in lowered:
        return False
    return bool(locs) and all(_looks_like_sitemap_url(loc) for loc in locs)


def _looks_like_sitemap_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "sitemap" in path or path.endswith((".xml", ".xml.gz"))


def _extract_links(base_url: str, html: str) -> list[str]:
    links: list[str] = []
    for raw in _HREF_RE.findall(html):
        href = raw.strip()
        if not href:
            continue
        if href.startswith(_IGNORED_SCHEMES):
            continue
        absolute = urljoin(base_url, href)
        _append_unique(links, absolute)
    return links


def _matches_filters(target: str, filters: _UrlFilters) -> bool:
    if not _matches_domain_filters(target, filters):
        return False
    path = urlparse(target).path or "/"
    if filters.select_paths and not any(
        re.search(pattern, path) for pattern in filters.select_paths
    ):
        return False
    return not (
        filters.exclude_paths and any(re.search(pattern, path) for pattern in filters.exclude_paths)
    )


def _matches_domain_filters(target: str, filters: _UrlFilters) -> bool:
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower()
    if filters.same_host_only and filters.base_host and host != filters.base_host:
        return False
    if filters.select_domains and not any(
        host == domain.lower() or host.endswith("." + domain.lower())
        for domain in filters.select_domains
    ):
        return False
    return not (
        filters.exclude_domains
        and any(
            host == domain.lower() or host.endswith("." + domain.lower())
            for domain in filters.exclude_domains
        )
    )


def _append_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _coerce_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, result))


def _coerce_float(value: Any, *, default: float, lo: float, hi: float) -> float:
    try:
        result = float(value)
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


class _UrlFilters:
    def __init__(
        self,
        *,
        base_host: str,
        same_host_only: bool,
        select_domains: list[str],
        exclude_domains: list[str],
        select_paths: list[str],
        exclude_paths: list[str],
    ) -> None:
        self.base_host = base_host
        self.same_host_only = same_host_only
        self.select_domains = select_domains
        self.exclude_domains = exclude_domains
        self.select_paths = select_paths
        self.exclude_paths = exclude_paths
