"""Tests for the browser fetch backend and auto-fallback wiring."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cognis.models.tool import ToolResult
from cognis.tools.executor.browser.manager import BROWSER_MANAGER_KEY, BrowserManager
from cognis.tools.executor.web.backends import (
    BrowserFetchBackend,
    available_fetch_backends,
    available_search_backends,
    get_browser_fetch_backend,
    resolve_fetch_backend,
    resolve_search_backend,
)

# ---------------------------------------------------------------------------
# Resolver split between search and fetch axes
# ---------------------------------------------------------------------------


def test_resolve_fetch_picks_split_key_over_legacy() -> None:
    metadata = {
        "web_backend": "tavily",
        "web_search_backend": "tavily",
        "web_fetch_backend": "direct",
        "web_secrets": {"tavily_api_key": "k"},
    }
    backend = resolve_fetch_backend(metadata)
    assert type(backend).__name__ == "DirectBackend"


def test_resolve_search_picks_split_key_over_legacy() -> None:
    metadata = {
        "web_backend": "direct",
        "web_search_backend": "brave",
        "web_fetch_backend": "direct",
        "web_secrets": {"brave_api_key": "k"},
    }
    backend = resolve_search_backend(metadata)
    assert type(backend).__name__ == "BraveBackend"


def test_resolve_search_falls_back_to_legacy_when_split_missing() -> None:
    metadata = {
        "web_backend": "tavily",
        "web_secrets": {"tavily_api_key": "k"},
    }
    backend = resolve_search_backend(metadata)
    assert type(backend).__name__ == "TavilyBackend"


def test_resolve_fetch_brave_falls_back_to_direct() -> None:
    metadata = {
        "web_fetch_backend": "brave",
        "web_secrets": {"brave_api_key": "k"},
    }
    backend = resolve_fetch_backend(metadata)
    # Brave has no fetch; fall through to direct.
    assert type(backend).__name__ == "DirectBackend"


def test_available_fetch_backends_includes_browser_when_manager_present() -> None:
    metadata = {BROWSER_MANAGER_KEY: BrowserManager(enabled=True)}
    fetch = available_fetch_backends(metadata)
    assert "browser" in fetch


def test_available_fetch_backends_omits_browser_when_manager_disabled() -> None:
    metadata = {BROWSER_MANAGER_KEY: BrowserManager(enabled=False)}
    fetch = available_fetch_backends(metadata)
    assert "browser" not in fetch


def test_available_search_backends_includes_searxng_when_url_set() -> None:
    metadata = {"web_searxng_url": "http://localhost:8888"}
    search = available_search_backends(metadata)
    assert "searxng" in search


# ---------------------------------------------------------------------------
# BrowserFetchBackend
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, html: str) -> None:
        self._html = html

    async def content(self) -> str:
        return self._html

    async def goto(self, url: str) -> None:  # pragma: no cover - unused here
        return None


class _FakeSession:
    def __init__(self, html: str) -> None:
        self.page = _FakePage(html)


class _FakeManager:
    def __init__(
        self,
        *,
        html: str = "<html><body><h1>OK</h1><p>content</p></body></html>",
        max_sessions: int = 4,
        raise_on_open: Exception | None = None,
    ) -> None:
        self._html = html
        self.max_sessions = max_sessions
        self._raise = raise_on_open
        self.opened: list[dict[str, Any]] = []
        self.closed: list[str] = []

    async def open_session(
        self,
        *,
        session_id: str,
        url: str,
        headless: bool = True,
        profile_mode: str = "default",
        wait_for_slot: bool = False,
        wait_timeout_seconds: float = 30.0,
        **_: Any,
    ) -> Any:
        self.opened.append(
            {
                "session_id": session_id,
                "url": url,
                "headless": headless,
                "profile_mode": profile_mode,
                "wait_for_slot": wait_for_slot,
                "wait_timeout_seconds": wait_timeout_seconds,
            }
        )
        if self._raise is not None:
            raise self._raise
        return _FakeSession(self._html)

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)


@pytest.mark.asyncio
async def test_browser_fetch_returns_markdown_via_trafilatura() -> None:
    manager = _FakeManager(html="<html><body><h1>Headline</h1><p>Body text here.</p></body></html>")
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("https://example.com", output_format="markdown")
    assert not result.is_error
    assert "Headline" in result.output or "headline" in result.output.lower()
    assert manager.opened[0]["wait_for_slot"] is True
    assert manager.opened[0]["headless"] is True
    assert manager.closed == [manager.opened[0]["session_id"]]
    assert (result.metadata or {}).get("browser_fetch") is True


@pytest.mark.asyncio
async def test_browser_fetch_returns_html_passthrough() -> None:
    manager = _FakeManager(html="<html><body>raw</body></html>")
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("https://example.com", output_format="html")
    assert "<html>" in result.output


@pytest.mark.asyncio
async def test_browser_fetch_propagates_pool_timeout_as_error() -> None:
    manager = _FakeManager(raise_on_open=TimeoutError("no slot"))
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("https://example.com")
    assert result.is_error
    assert "session slot" in result.output
    assert (result.metadata or {}).get("browser_pool_timeout") is True
    assert manager.closed == []


@pytest.mark.asyncio
async def test_browser_fetch_handles_runtime_unavailable() -> None:
    manager = _FakeManager(raise_on_open=RuntimeError("Browser runtime disabled"))
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("https://example.com")
    assert result.is_error
    assert "Browser runtime unavailable" in result.output


@pytest.mark.asyncio
async def test_browser_fetch_rejects_invalid_url() -> None:
    manager = _FakeManager()
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("ftp://example.com/file")
    assert result.is_error
    assert "Unsupported URL" in result.output
    assert manager.opened == []


# ---------------------------------------------------------------------------
# get_browser_fetch_backend wiring
# ---------------------------------------------------------------------------


def test_get_browser_fetch_backend_respects_disabled_manager() -> None:
    metadata = {BROWSER_MANAGER_KEY: BrowserManager(enabled=False)}
    assert get_browser_fetch_backend(metadata) is None


def test_get_browser_fetch_backend_caches_per_manager() -> None:
    manager = BrowserManager(enabled=True)
    metadata = {BROWSER_MANAGER_KEY: manager}
    a = get_browser_fetch_backend(metadata)
    b = get_browser_fetch_backend(metadata)
    assert a is b
    # New manager triggers a new backend instance.
    new_manager = BrowserManager(enabled=True)
    metadata[BROWSER_MANAGER_KEY] = new_manager
    c = get_browser_fetch_backend(metadata)
    assert c is not a


# ---------------------------------------------------------------------------
# BrowserManager wait_for_slot semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_slot_false_raises_immediately_when_full() -> None:
    manager = BrowserManager(max_sessions=1)
    manager._open_in_flight = 1  # noqa: SLF001 — simulate one outstanding open
    with pytest.raises(RuntimeError, match="session limit"):
        await manager._reserve_open_slot(  # noqa: SLF001
            headless=True,
            wait_for_slot=False,
            wait_timeout_seconds=0,
        )


@pytest.mark.asyncio
async def test_wait_for_slot_true_blocks_until_slot_available() -> None:
    manager = BrowserManager(max_sessions=1)
    manager._open_in_flight = 1  # noqa: SLF001
    queue_started = asyncio.Event()
    queue_finished = asyncio.Event()

    async def waiter() -> None:
        queue_started.set()
        await manager._reserve_open_slot(  # noqa: SLF001
            headless=True,
            wait_for_slot=True,
            wait_timeout_seconds=2.0,
        )
        queue_finished.set()

    task = asyncio.create_task(waiter())
    await queue_started.wait()
    # The waiter should still be blocked.
    await asyncio.sleep(0.05)
    assert not queue_finished.is_set()

    # Free the simulated slot; the waiter should pick it up shortly.
    manager._open_in_flight = 0  # noqa: SLF001
    await asyncio.wait_for(queue_finished.wait(), timeout=1.0)
    await task


@pytest.mark.asyncio
async def test_wait_for_slot_times_out_when_pool_stays_full() -> None:
    manager = BrowserManager(max_sessions=1)
    manager._open_in_flight = 1  # noqa: SLF001
    with pytest.raises(TimeoutError):
        await manager._reserve_open_slot(  # noqa: SLF001
            headless=True,
            wait_for_slot=True,
            wait_timeout_seconds=0.1,
        )


# ---------------------------------------------------------------------------
# Auto-fallback in handle_web_fetch
# ---------------------------------------------------------------------------


class _FakeContext:
    def __init__(self, runtime_metadata: dict[str, Any]) -> None:
        self.runtime_metadata = runtime_metadata


@pytest.mark.asyncio
async def test_handle_web_fetch_auto_fallback_uses_browser_on_cloudflare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    primary_calls: list[str] = []
    browser_calls: list[str] = []

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            primary_calls.append(url)
            return ToolResult(
                output="This site is protected by Cloudflare and requires browser access.",
                is_error=True,
                metadata={"cloudflare_blocked": True},
            )

    class _FakeBrowser:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            browser_calls.append(url)
            return ToolResult(output="rendered content")

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeBrowser(),
    )

    metadata: dict[str, Any] = {
        "web_fetch_backend": "direct",
        "web_fetch_fallback_browser": True,
    }
    ctx = _FakeContext(metadata)
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)

    assert primary_calls == ["https://example.com"]
    assert browser_calls == ["https://example.com"]
    assert not result.is_error
    assert "rendered content" in result.output
    assert "[[page:1]]" in result.output
    assert (result.metadata or {}).get("browser_fallback") is True
    assert (result.metadata or {}).get("stored_output")
    assert (result.metadata or {}).get("output_anchors")


@pytest.mark.asyncio
async def test_handle_web_fetch_explicit_direct_still_allows_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="This site is protected by Cloudflare.",
                is_error=True,
                metadata={"cloudflare_blocked": True},
            )

    browser_used = False

    class _FakeBrowser:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            nonlocal browser_used
            browser_used = True
            return ToolResult(output="rendered content")

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeBrowser(),
    )

    ctx = _FakeContext(
        {
            "web_fetch_backend": "direct",
            "web_fetch_fallback_browser": True,
        }
    )
    result = await handlers.handle_web_fetch(
        {"url": "https://example.com", "backend": "direct"}, ctx
    )
    assert not result.is_error
    assert browser_used is True


@pytest.mark.asyncio
async def test_handle_web_fetch_disabled_fallback_returns_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="HTTP 503: Service Unavailable",
                is_error=True,
            )

    browser_used = False

    class _FakeBrowser:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            nonlocal browser_used
            browser_used = True
            return ToolResult(output="rendered content")

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeBrowser(),
    )

    ctx = _FakeContext(
        {
            "web_fetch_backend": "direct",
            "web_fetch_fallback_browser": False,
        }
    )
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)
    assert result.is_error
    assert browser_used is False


@pytest.mark.asyncio
async def test_handle_web_fetch_no_fallback_for_4xx_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            # 404 / 401 are not browser-fixable, no fallback.
            return ToolResult(output="HTTP 404: Not Found", is_error=True)

    browser_used = False

    class _FakeBrowser:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            nonlocal browser_used
            browser_used = True
            return ToolResult(output="rendered content")

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeBrowser(),
    )

    ctx = _FakeContext({"web_fetch_backend": "direct"})
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)
    assert result.is_error
    assert "404" in result.output
    assert browser_used is False
