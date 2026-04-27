"""Tests for the in-tree web crawler + sitemap discovery."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.concurrency import WebConcurrencyController
from cognis.tools.executor.web.crawler import (
    _coerce_int,
    _coerce_str_list,
    _matches_filters,
    crawl_site,
)

# ---------------------------------------------------------------------------
# Argument coercion + filtering helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 5),
        ("garbage", 5),
        (0, 1),
        (-3, 1),
        (4, 4),
        (200, 100),
    ],
)
def test_coerce_int_clamps_within_range(value: Any, expected: int) -> None:
    assert _coerce_int(value, default=5, lo=1, hi=100) == expected


def test_coerce_str_list_handles_csv_and_iterables() -> None:
    assert _coerce_str_list(None) == []
    assert _coerce_str_list("a, b ,, c") == ["a", "b", "c"]
    assert _coerce_str_list(["x", " y ", ""]) == ["x", "y"]


def test_matches_filters_respects_same_host_default() -> None:
    assert (
        _matches_filters(
            target="https://example.com/path",
            base_host="example.com",
            same_host_only=True,
            select_domains=[],
            exclude_domains=[],
            select_paths=[],
            exclude_paths=[],
        )
        is True
    )
    assert (
        _matches_filters(
            target="https://other.example/path",
            base_host="example.com",
            same_host_only=True,
            select_domains=[],
            exclude_domains=[],
            select_paths=[],
            exclude_paths=[],
        )
        is False
    )


def test_matches_filters_select_and_exclude_paths() -> None:
    base = dict(
        base_host="example.com",
        same_host_only=True,
        select_domains=[],
        exclude_domains=[],
    )
    assert (
        _matches_filters(
            target="https://example.com/docs/intro",
            select_paths=[r"^/docs"],
            exclude_paths=[],
            **base,
        )
        is True
    )
    assert (
        _matches_filters(
            target="https://example.com/blog/post",
            select_paths=[r"^/docs"],
            exclude_paths=[],
            **base,
        )
        is False
    )
    assert (
        _matches_filters(
            target="https://example.com/internal/secret",
            select_paths=[],
            exclude_paths=[r"/internal"],
            **base,
        )
        is False
    )


# ---------------------------------------------------------------------------
# crawl_site
# ---------------------------------------------------------------------------


class _FakeFetchBackend:
    """Returns a deterministic markdown body keyed by URL."""

    def __init__(self, *, html_body: dict[str, str]) -> None:
        self._html_body = html_body
        self.calls: list[str] = []

    async def fetch(self, url: str, **_kwargs: Any) -> ToolResult:
        self.calls.append(url)
        body = self._html_body.get(url, "")
        return ToolResult(output=body)


@pytest.mark.asyncio
async def test_crawl_site_visits_each_url_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_backend = _FakeFetchBackend(
        html_body={
            "https://example.com/": "ok root",
            "https://example.com/about": "about page",
            "https://example.com/contact": "contact page",
        }
    )

    async def _fake_discover(target: str, *, page_timeout: int) -> list[str]:
        del page_timeout
        if target == "https://example.com/":
            return [
                "https://example.com/about",
                "https://example.com/contact",
                "https://example.com/about",  # duplicate
                "https://other.example/away",  # filtered out
            ]
        return []

    monkeypatch.setattr("cognis.tools.executor.web.crawler._discover_links", _fake_discover)

    controller = WebConcurrencyController()
    result = await crawl_site(
        url="https://example.com/",
        fetch_backend=fake_backend,
        backend_label="direct",
        controller=controller,
        options={"max_depth": 1, "max_breadth": 5, "limit": 10},
    )

    assert not result.is_error
    assert (result.metadata or {}).get("crawl_pages") == 3
    assert "[[page:1]]" in result.output
    assert (result.metadata or {}).get("stored_output")
    assert (result.metadata or {}).get("output_anchors")
    assert sorted(fake_backend.calls) == [
        "https://example.com/",
        "https://example.com/about",
        "https://example.com/contact",
    ]


@pytest.mark.asyncio
async def test_crawl_site_respects_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_backend = _FakeFetchBackend(
        html_body={f"https://example.com/p{i}": f"page {i}" for i in range(20)}
    )

    async def _fake_discover(target: str, *, page_timeout: int) -> list[str]:
        del page_timeout
        return [f"https://example.com/p{i}" for i in range(20)]

    monkeypatch.setattr("cognis.tools.executor.web.crawler._discover_links", _fake_discover)

    controller = WebConcurrencyController()
    result = await crawl_site(
        url="https://example.com/",
        fetch_backend=fake_backend,
        backend_label="direct",
        controller=controller,
        options={"max_depth": 2, "max_breadth": 50, "limit": 4},
    )
    # 4 pages crawled (root + 3 children up to limit).
    assert (result.metadata or {}).get("crawl_pages") == 4
    assert (result.metadata or {}).get("output_anchors")
    assert len(fake_backend.calls) == 4


@pytest.mark.asyncio
async def test_crawl_site_rejects_invalid_url() -> None:
    fake_backend = _FakeFetchBackend(html_body={})
    controller = WebConcurrencyController()
    result = await crawl_site(
        url="ftp://example.com/file",
        fetch_backend=fake_backend,
        backend_label="direct",
        controller=controller,
        options={},
    )
    assert result.is_error
    assert "Unsupported URL" in result.output


# ---------------------------------------------------------------------------
# sitemap discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_sitemap_urls_prefers_sitemap_xml() -> None:
    from cognis.tools.executor.web.sitemap import discover_sitemap_urls

    sitemap_xml = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
  <url><loc>https://other.example/x</loc></url>
</urlset>"""

    request = httpx.Request("GET", "https://example.com/sitemap.xml")

    def _make_response(status: int, text: str = "") -> httpx.Response:
        return httpx.Response(status, request=request, text=text)

    responses = {
        "https://example.com/sitemap.xml": _make_response(200, sitemap_xml),
        "https://example.com/sitemap_index.xml": _make_response(404),
    }

    async def _get(url: str) -> httpx.Response:
        return responses[url]

    with patch("cognis.tools.executor.web.sitemap.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=_get)
        mock_client_cls.return_value = mock_client

        urls, source = await discover_sitemap_urls("https://example.com/", limit=10)

    assert source == "sitemap"
    # other.example is filtered by same_host_only.
    assert urls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
async def test_discover_sitemap_falls_back_to_html_links_when_no_sitemap() -> None:
    from cognis.tools.executor.web.sitemap import discover_sitemap_urls

    page_html = (
        "<html><body>"
        "<a href='/about'>About</a>"
        "<a href='https://example.com/blog/post'>Post</a>"
        "<a href='mailto:hi@example.com'>Email</a>"
        "<a href='https://other.example/x'>External</a>"
        "</body></html>"
    )
    request = httpx.Request("GET", "https://example.com/")
    responses = {
        "https://example.com/sitemap.xml": httpx.Response(404, request=request),
        "https://example.com/sitemap_index.xml": httpx.Response(404, request=request),
        "https://example.com/": httpx.Response(200, request=request, text=page_html),
    }

    async def _get(url: str) -> httpx.Response:
        return responses[url]

    with patch("cognis.tools.executor.web.sitemap.httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=_get)
        mock_client_cls.return_value = mock_client

        urls, source = await discover_sitemap_urls("https://example.com/", limit=10)

    assert source == "html_links"
    assert "https://example.com/about" in urls
    assert "https://example.com/blog/post" in urls
    assert "mailto:hi@example.com" not in urls
    assert "https://other.example/x" not in urls


# ---------------------------------------------------------------------------
# Handler routing: Tavily fast path vs DIY
# ---------------------------------------------------------------------------


class _FakeContext:
    def __init__(self, runtime_metadata: dict[str, Any]) -> None:
        self.runtime_metadata = runtime_metadata


@pytest.mark.asyncio
async def test_handle_web_crawl_uses_diy_when_fetch_backend_not_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    diy_called = False

    async def _fake_crawl_site(**kwargs: Any) -> ToolResult:
        nonlocal diy_called
        diy_called = True
        return ToolResult(output="diy crawl ok")

    monkeypatch.setattr(
        "cognis.tools.executor.web.crawler.crawl_site",
        _fake_crawl_site,
    )

    ctx = _FakeContext({"web_fetch_backend": "direct"})
    result = await handlers.handle_web_crawl({"url": "https://example.com"}, ctx)
    assert diy_called
    assert result.output == "diy crawl ok"


@pytest.mark.asyncio
async def test_handle_web_crawl_uses_tavily_when_fetch_backend_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakeTavily:
        async def crawl(self, url: str, *, options: Any = None) -> ToolResult:
            return ToolResult(output="tavily crawl ok")

    monkeypatch.setattr(
        handlers,
        "_require_tavily",
        lambda ctx: _FakeTavily(),
    )

    ctx = _FakeContext({"web_fetch_backend": "tavily"})
    result = await handlers.handle_web_crawl({"url": "https://example.com"}, ctx)
    assert result.output == "tavily crawl ok"


@pytest.mark.asyncio
async def test_handle_web_map_uses_sitemap_when_fetch_backend_not_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    async def _fake_discover(
        url: str, *, limit: int = 200, same_host_only: bool = True
    ) -> tuple[list[str], str]:
        del limit, same_host_only
        return ["https://example.com/a", "https://example.com/b"], "sitemap"

    monkeypatch.setattr(
        "cognis.tools.executor.web.sitemap.discover_sitemap_urls",
        _fake_discover,
    )

    ctx = _FakeContext({"web_fetch_backend": "direct"})
    result = await handlers.handle_web_map({"url": "https://example.com"}, ctx)
    assert not result.is_error
    assert "https://example.com/a" in result.output
    assert (result.metadata or {}).get("sitemap_source") == "sitemap"
    assert (result.metadata or {}).get("url_count") == 2


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------


def test_research_skill_is_registered() -> None:
    from cognis.core.system_skills import (
        SYSTEM_SKILL_DEFAULTS,
        get_system_skill_default,
    )

    assert "cognis-web-research" in SYSTEM_SKILL_DEFAULTS
    rendered = get_system_skill_default("cognis-web-research")
    assert rendered is not None
    assert rendered["skill_id"] == "cognis-web-research"
    assert "web_search" in str(rendered["instructions"])
    assert "web_fetch" in str(rendered["instructions"])
    linked = rendered["linked_tool_ids"]
    assert isinstance(linked, list) and "builtin:web_search" in linked
