"""Sitemap discovery for ``web_map``.

Tries ``/sitemap.xml`` and ``/sitemap_index.xml`` first; falls back to
enumerating ``<a href>`` links discovered on the start page when no
sitemap is available. Always uses the resolved fetch backend (with
auto-browser fallback inherited) so JS-only sites still surface usable
links.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from cognis.tools.executor.web.headers import BROWSER_HEADERS, sanitise_url

logger = logging.getLogger(__name__)

_SITEMAP_TIMEOUT_SECONDS = 15.0
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_IGNORED_SCHEMES = ("javascript:", "mailto:", "tel:", "#")


async def discover_sitemap_urls(
    base_url: str,
    *,
    limit: int = 200,
    same_host_only: bool = True,
) -> tuple[list[str], str]:
    """Discover URLs reachable from ``base_url`` via sitemap.xml or HTML links.

    Returns ``(urls, source)`` where ``source`` is one of
    ``"sitemap"``, ``"sitemap_index"`` or ``"html_links"``.
    """
    base_url = sanitise_url(base_url)
    base_host = (urlparse(base_url).hostname or "").lower()
    candidate_paths = ["/sitemap.xml", "/sitemap_index.xml"]
    seen: list[str] = []
    sitemap_source: str | None = None

    async with httpx.AsyncClient(
        timeout=_SITEMAP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=BROWSER_HEADERS,
    ) as client:
        for path in candidate_paths:
            sitemap_url = urljoin(base_url, path)
            try:
                response = await client.get(sitemap_url)
            except httpx.RequestError:
                continue
            if response.status_code != 200:
                continue
            urls_in_sitemap = _LOC_RE.findall(response.text or "")
            if urls_in_sitemap:
                sitemap_source = "sitemap_index" if "index" in path else "sitemap"
                for url in urls_in_sitemap:
                    cleaned = url.strip()
                    if not cleaned:
                        continue
                    if (
                        same_host_only
                        and base_host
                        and (urlparse(cleaned).hostname or "").lower() != base_host
                    ):
                        continue
                    if cleaned not in seen:
                        seen.append(cleaned)
                    if len(seen) >= max(1, limit):
                        break
                if seen:
                    return seen[:limit], sitemap_source

        # Fallback: enumerate href links from the start page.
        try:
            response = await client.get(base_url)
        except httpx.RequestError as exc:
            logger.debug("web: sitemap fallback fetch failed: %s", exc)
            return [], "html_links"

        if response.status_code >= 400:
            return [], "html_links"
        for raw in _HREF_RE.findall(response.text or ""):
            href = raw.strip()
            if not href:
                continue
            if href.startswith(_IGNORED_SCHEMES):
                continue
            absolute = urljoin(base_url, href)
            if (
                same_host_only
                and base_host
                and (urlparse(absolute).hostname or "").lower() != base_host
            ):
                continue
            if absolute not in seen:
                seen.append(absolute)
            if len(seen) >= max(1, limit):
                break

    return seen[:limit], "html_links"
