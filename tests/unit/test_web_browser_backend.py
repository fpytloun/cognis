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
    get_headed_browser_fetch_backend,
    get_preferred_browser_fetch_backend,
    resolve_fetch_backend,
    resolve_search_backend,
)
from cognis.tools.executor.web.backends.browser import _classify_browser_extract_quality

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
    def __init__(self, html: str, *, url: str = "https://example.com") -> None:
        self._html = html
        self.url = url
        self.wait_for_function_calls = 0
        self.goto_calls: list[dict[str, Any]] = []
        self.evaluate_calls = 0

    async def content(self) -> str:
        return self._html

    async def wait_for_function(self, *_args: Any, **_kwargs: Any) -> None:
        self.wait_for_function_calls += 1

    async def evaluate(self, *_args: Any, **_kwargs: Any) -> bool:
        self.evaluate_calls += 1
        return False

    async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append({"url": url, **kwargs})
        self.url = url


class _FakeSession:
    def __init__(self, html: str, *, url: str = "https://example.com") -> None:
        self.page = _FakePage(html, url=url)


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
        **extra: Any,
    ) -> Any:
        self.opened.append(
            {
                "session_id": session_id,
                "url": url,
                "headless": headless,
                "profile_mode": profile_mode,
                "wait_for_slot": wait_for_slot,
                "wait_timeout_seconds": wait_timeout_seconds,
                **extra,
            }
        )
        if self._raise is not None:
            raise self._raise
        return _FakeSession(self._html, url=url)

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)


class _ConsentRedirectPage(_FakePage):
    def __init__(self) -> None:
        super().__init__(
            "<html><body><h1>Ochrana osobních údajů</h1>"
            "<button>Rozumím a souhlasím</button></body></html>",
            url="https://heureka.group/cs/podminky-pouzivani/ochrana-osobnich-udaju",
        )

    async def evaluate(self, *_args: Any, **_kwargs: Any) -> bool:
        self.evaluate_calls += 1
        return True

    async def goto(self, url: str, **kwargs: Any) -> None:
        await super().goto(url, **kwargs)
        self._html = (
            "<html><body><h1>HiPP BIO Snackie</h1><p>Product content loaded.</p></body></html>"
        )


class _ConsentRedirectSession:
    def __init__(self) -> None:
        self.page = _ConsentRedirectPage()


class _ConsentRedirectManager(_FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.session: _ConsentRedirectSession | None = None

    async def open_session(self, **kwargs: Any) -> Any:
        self.opened.append(kwargs)
        self.session = _ConsentRedirectSession()
        return self.session


@pytest.mark.asyncio
async def test_browser_fetch_returns_markdown_via_trafilatura() -> None:
    manager = _FakeManager(html="<html><body><h1>Headline</h1><p>Body text here.</p></body></html>")
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("https://example.com", output_format="markdown")
    assert not result.is_error
    assert "Headline" in result.output or "headline" in result.output.lower()
    assert manager.opened[0]["wait_for_slot"] is True
    assert manager.opened[0]["headless"] is True
    assert manager.opened[0]["lifecycle"] == "ephemeral"
    assert manager.opened[0]["navigation_timeout_seconds"] == 60.0
    assert manager.opened[0]["wait_until"] == "domcontentloaded"
    assert manager.opened[0]["network_idle_after_dom_seconds"] == 3.0
    assert manager.closed == [manager.opened[0]["session_id"]]
    assert (result.metadata or {}).get("browser_fetch") is True


@pytest.mark.asyncio
async def test_browser_fetch_passes_navigation_settings() -> None:
    manager = _FakeManager(html="<html><body><h1>Headline</h1><p>Body text here.</p></body></html>")
    backend = BrowserFetchBackend(
        manager,  # type: ignore[arg-type]
        navigation_timeout_seconds=75,
        wait_until="load",
        network_idle_after_dom_seconds=0,
    )

    await backend.fetch("https://example.com")

    assert manager.opened[0]["navigation_timeout_seconds"] == 75
    assert manager.opened[0]["wait_until"] == "load"
    assert manager.opened[0]["network_idle_after_dom_seconds"] == 0


def test_classify_browser_extract_quality_flags_empty_block() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "empty", "extraction_score": 0.0, "title": "reuters.com"},
        "",
    )
    assert signal == "empty_extraction"


def test_classify_browser_extract_quality_flags_interstitial() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "readability", "extraction_score": 120.0},
        "Verify you are human before continuing. Cloudflare Turnstile challenge page.",
    )
    assert signal == "interstitial"


def test_classify_browser_extract_quality_flags_cloudflare_challenge() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "html", "extraction_score": 120.0, "title": "Just a moment..."},
        "Checking if the site connection is secure. Cloudflare Ray ID: 1234",
    )
    assert signal == "interstitial"


def test_classify_browser_extract_quality_ignores_normal_cloudflare_scripts() -> None:
    content = " ".join(
        [
            "<html><head><title>Hipp | notino.cz</title></head><body>",
            "<h1>Hipp</h1><h2>Babysanft Sensitive</h2>",
            "<p>ochranny krem na kazdodenni peci o zadecek 75 ml</p>",
            "<script src='/googletaggatewayall/UwLjdStQR3wE5nf'></script>",
            "<script>window.cloudflareAnalytics = true;</script>",
            "</body></html>",
        ]
    )

    signal = _classify_browser_extract_quality(
        {"extractor": "html", "extraction_score": 4952.0, "title": "Hipp | notino.cz"},
        content,
    )

    assert signal is None


def test_classify_browser_extract_quality_ignores_drmax_turnstile_product_page() -> None:
    content = " ".join(
        [
            "<html><head><title>Hipp BIO SNACKIE 120 g | Dr. Max Lekarna</title>",
            '<meta name="description" content="Hipp BIO SNACKIE je ovocna kapsicka idealni pro aktivni deti.">',
            '<link rel="canonical" href="https://www.drmax.cz/hipp-bio-sport-hruska-pomer-mango-banan-ryze-120g">',
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>',
            "</head><body><main><h1>Hipp BIO SNACKIE hruška-pomeranč-mango-banán-rýže 120 g</h1>",
            '<script type="application/ld+json">{"@context":"https://schema.org/","@type":"Product",',
            '"name":"Hipp BIO SNACKIE hruška-pomeranč-mango-banán-rýže 120 g"}</script>',
            "<p>Ovocna kapsicka pro deti od ukonceneho 1. roku.</p></main></body></html>",
        ]
    )

    signal = _classify_browser_extract_quality(
        {
            "extractor": "html",
            "extraction_score": 24598.0,
            "title": "Hipp BIO SNACKIE hruška-pomeranč-mango-banán-rýže 120 g | Dr. Max Lékárna",
            "description": "Hipp BIO SNACKIE je ovocna kapsicka idealni pro aktivni deti.",
        },
        content,
        requested_url="https://www.drmax.cz/hipp-bio-sport-hruska-pomer-mango-banan-ryze-120g",
        final_url="https://www.drmax.cz/hipp-bio-sport-hruska-pomer-mango-banan-ryze-120g",
    )

    assert signal is None


def test_classify_browser_extract_quality_flags_reddit_network_security_page() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "trafilatura_recall", "extraction_score": 41.4},
        "You've been blocked by network security. File a ticket.",
        requested_url="https://www.reddit.com/r/quails/comments/t8r4dd/example/",
        final_url="https://www.reddit.com/r/quails/comments/t8r4dd/example/?js_challenge=1&token=abc",
    )

    assert signal == "interstitial"


def test_classify_browser_extract_quality_flags_datadome_captcha() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "html", "extraction_score": 120.0},
        '<iframe title="DataDome CAPTCHA" src="https://geo.captcha-delivery.com/captcha/">',
    )
    assert signal == "interstitial"


def test_classify_browser_extract_quality_flags_multilingual_cookie_interstitial() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "trafilatura_precision", "extraction_score": 120.0},
        " ".join(
            [
                "Provozovatel teto webove stranky pouziva cookies pro zajisteni funkcnosti.",
                "S vasim souhlasem take pro lepsi uzivatelsky zazitek a ochranu soukromi.",
                "Kliknutim na tlacitko Rozumim a souhlasim vyjadrite souhlas.",
                "Pouze nezbytne cookies odmitnou personalizaci a analytiku.",
            ]
            * 12
        ),
    )
    assert signal == "consent_interstitial"


def test_classify_browser_extract_quality_flags_cross_origin_consent_redirect() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "trafilatura_precision", "extraction_score": 7000.0},
        "Ochrana osobních údajů na portálech Heureka Group a.s. Tento dokument popisuje GDPR.",
        requested_url="https://maso-zeleninove-prikrmy.heureka.cz/product/",
        final_url="https://heureka.group/cs/podminky-pouzivani/ochrana-osobnich-udaju",
    )
    assert signal == "consent_redirect"


def test_classify_browser_extract_quality_flags_same_origin_cookie_redirect() -> None:
    signal = _classify_browser_extract_quality(
        {"extractor": "readability", "extraction_score": 900.0, "title": "Prohlaseni o cookies"},
        "Tento dokument popisuje cookies, souhlas, privacy a gdpr pro tento web.",
        requested_url="https://www.drmax.cz/hipp-bio-sport-hruska-pomer-mango-banan-ryze-120g",
        final_url="https://www.drmax.cz/obecne/pouzivani-cookies",
    )
    assert signal == "consent_redirect"


def test_classify_browser_extract_quality_allows_same_origin_terms_page() -> None:
    content = " ".join(
        [
            "<html><head><title>Terms and conditions | Example</title>",
            '<meta name="description" content="Terms and conditions for Example purchases and services.">',
            '<link rel="canonical" href="https://example.com/terms-and-conditions">',
            "</head><body><main><h1>Terms and conditions</h1>",
            "<p>These terms describe product orders, delivery, refunds, and service conditions.</p>",
            "</main></body></html>",
        ]
    )

    signal = _classify_browser_extract_quality(
        {
            "extractor": "html",
            "extraction_score": 1200.0,
            "title": "Terms and conditions | Example",
            "description": "Terms and conditions for Example purchases and services.",
        },
        content,
        requested_url="https://example.com/legal",
        final_url="https://example.com/terms-and-conditions",
    )

    assert signal is None


def test_classify_browser_extract_quality_allows_same_origin_privacy_page() -> None:
    content = " ".join(
        [
            "<html><head><title>Privacy Policy | Example</title>",
            '<meta name="description" content="Privacy Policy explaining GDPR data handling for customers.">',
            '<link rel="canonical" href="https://example.com/privacy-policy">',
            "</head><body><main><h1>Privacy Policy</h1>",
            "<p>This privacy policy explains GDPR, consent, account data, and customer rights.</p>",
            "</main></body></html>",
        ]
    )

    signal = _classify_browser_extract_quality(
        {
            "extractor": "html",
            "extraction_score": 1200.0,
            "title": "Privacy Policy | Example",
            "description": "Privacy Policy explaining GDPR data handling for customers.",
        },
        content,
        requested_url="https://example.com/legal",
        final_url="https://example.com/privacy-policy",
    )

    assert signal is None


@pytest.mark.asyncio
async def test_browser_fetch_returns_html_passthrough() -> None:
    manager = _FakeManager(html="<html><body>raw</body></html>")
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]
    result = await backend.fetch("https://example.com", output_format="html")
    assert "<html>" in result.output


@pytest.mark.asyncio
async def test_browser_fetch_recovers_consent_redirect_and_renavigates() -> None:
    manager = _ConsentRedirectManager()
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]

    result = await backend.fetch(
        "https://maso-zeleninove-prikrmy.heureka.cz/hipp-bio-snackie/",
        output_format="markdown",
    )

    assert not result.is_error
    assert "HiPP BIO Snackie" in result.output
    assert manager.session is not None
    assert manager.session.page.evaluate_calls == 1
    assert manager.session.page.goto_calls[0]["url"] == (
        "https://maso-zeleninove-prikrmy.heureka.cz/hipp-bio-snackie/"
    )
    extracted = (result.metadata or {}).get("extracted_document") or {}
    assert extracted.get("browser_consent_redirect_recovered") is True


@pytest.mark.asyncio
async def test_browser_fetch_waits_for_rendered_body_before_extracting() -> None:
    manager = _FakeManager(html="<html><head><title>Shell</title></head><body></body></html>")
    backend = BrowserFetchBackend(manager)  # type: ignore[arg-type]

    result = await backend.fetch("https://example.com", output_format="html")

    assert not result.is_error
    assert manager.closed == [manager.opened[0]["session_id"]]


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


def test_preferred_browser_fetch_uses_headed_when_available() -> None:
    manager = BrowserManager(enabled=True, headed_allowed=True)
    metadata = {
        BROWSER_MANAGER_KEY: manager,
        "web_browser_fetch_headed_fallback_enabled": True,
    }

    backend = get_preferred_browser_fetch_backend(metadata)

    assert backend is get_headed_browser_fetch_backend(metadata)
    assert backend is not None
    assert backend.headed is True


def test_resolve_browser_fetch_prefers_headed_when_available() -> None:
    manager = BrowserManager(enabled=True, headed_allowed=True)
    metadata = {
        BROWSER_MANAGER_KEY: manager,
        "web_browser_fetch_headed_fallback_enabled": True,
    }

    backend = resolve_fetch_backend(metadata, "browser")

    assert isinstance(backend, BrowserFetchBackend)
    assert backend.headed is True


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
    def __init__(
        self,
        runtime_metadata: dict[str, Any],
        shared_runtime_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.runtime_metadata = runtime_metadata
        self.shared_runtime_metadata = shared_runtime_metadata or {}


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
async def test_handle_web_fetch_empty_verification_page_attempts_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="",
                metadata={
                    "extracted_document": {
                        "extractor": "empty",
                        "extraction_score": 0.0,
                        "title": "Reddit - Please wait for verification",
                    }
                },
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
        {"url": "https://www.reddit.com/r/quails/comments/t8r4dd/example/"}, ctx
    )

    assert not result.is_error
    assert browser_used is True
    assert "rendered content" in result.output


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
    assert "Browser fallback is disabled" in result.output
    metadata = result.metadata or {}
    assert metadata.get("browser_fallback_attempted") is False
    assert metadata.get("browser_fallback_skipped_reason") == "fallback_disabled"


@pytest.mark.asyncio
async def test_handle_web_fetch_no_fallback_for_4xx_user_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            # Plain missing pages are not browser-fixable, so 404 should not retry.
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


@pytest.mark.asyncio
async def test_handle_web_fetch_retries_401_through_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(output="HTTP 401: Unauthorized", is_error=True)

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
async def test_explicit_browser_backend_without_manager_returns_clear_error() -> None:
    from cognis.tools.executor.web import handlers
    from cognis.tools.executor.web.backends import resolve_fetch_backend

    metadata: dict[str, Any] = {"web_fetch_backend": "direct"}
    backend = resolve_fetch_backend(metadata, "browser")
    assert type(backend).__name__ == "_UnavailableBrowserBackend"

    ctx = _FakeContext(metadata)
    result = await handlers.handle_web_fetch(
        {"url": "https://example.com", "backend": "browser"}, ctx
    )
    assert result.is_error
    assert "Browser fetch backend is unavailable" in result.output


@pytest.mark.asyncio
async def test_handle_web_fetch_escalates_to_headed_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="Direct HTTP fetch was blocked by Cloudflare",
                is_error=True,
                metadata={"cloudflare_blocked": True},
            )

    class _FakeHeadless:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            raise AssertionError("headless fallback should not run when headed is available")

    headed_called = False

    class _FakeHeaded:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            nonlocal headed_called
            headed_called = True
            return ToolResult(output="rendered headed content")

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeHeadless(),
    )
    monkeypatch.setattr(
        handlers,
        "get_headed_browser_fetch_backend",
        lambda metadata: _FakeHeaded(),
    )

    ctx = _FakeContext(
        {
            "web_fetch_backend": "direct",
            "web_fetch_fallback_browser": True,
            "web_browser_fetch_headed_fallback_enabled": True,
        }
    )
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)

    assert not result.is_error
    assert headed_called is True
    metadata = result.metadata or {}
    assert metadata.get("browser_fallback") is True
    assert metadata.get("browser_fallback_mode") == "headed"
    assert metadata.get("browser_fallback_modes_attempted") == ["headed"]
    assert metadata.get("primary_backend") == "direct"


@pytest.mark.asyncio
async def test_handle_web_fetch_combined_diagnostic_when_all_fallbacks_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="Direct HTTP fetch was blocked by Cloudflare",
                is_error=True,
                metadata={"cloudflare_blocked": True, "direct_fetch_blocked": True},
            )

    class _FakeHeadless:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            raise AssertionError("headless fallback should not run when headed is available")

    class _FakeHeaded:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(output="headed still blocked", is_error=True)

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeHeadless(),
    )
    monkeypatch.setattr(
        handlers,
        "get_headed_browser_fetch_backend",
        lambda metadata: _FakeHeaded(),
    )

    ctx = _FakeContext(
        {
            "web_fetch_backend": "direct",
            "web_fetch_fallback_browser": True,
            "web_browser_fetch_headed_fallback_enabled": True,
        }
    )
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)

    assert result.is_error
    assert "direct fetch failed" in result.output.lower()
    assert "headed browser fallback failed" in result.output.lower()
    metadata = result.metadata or {}
    assert metadata.get("browser_fallback_attempted") is True
    assert metadata.get("browser_fallback_modes_attempted") == ["headed"]
    assert metadata.get("browser_fallback_success") is False
    assert metadata.get("cloudflare_blocked") is True


@pytest.mark.asyncio
async def test_headed_fallback_unavailable_uses_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="Direct HTTP fetch was blocked by Cloudflare",
                is_error=True,
                metadata={"cloudflare_blocked": True},
            )

    class _FakeHeadless:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(output="headless blocked", is_error=True)

    headed_called = False

    class _FakeHeaded:
        async def fetch(self, url: str, **_: Any) -> ToolResult:  # pragma: no cover - guard
            nonlocal headed_called
            headed_called = True
            return ToolResult(output="should not run", is_error=False)

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeHeadless(),
    )
    monkeypatch.setattr(
        handlers,
        "get_headed_browser_fetch_backend",
        lambda metadata: None,
    )

    ctx = _FakeContext(
        {
            "web_fetch_backend": "direct",
            "web_fetch_fallback_browser": True,
            "web_browser_fetch_headed_fallback_enabled": False,
        }
    )
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)

    assert result.is_error
    assert headed_called is False
    assert "headed browser fallback is disabled" in result.output.lower()
    metadata = result.metadata or {}
    assert metadata.get("browser_fallback_modes_attempted") == ["headless"]
    assert "headed_fallback_skipped_reason" in metadata


@pytest.mark.asyncio
async def test_handle_web_fetch_treats_headed_empty_extraction_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="Direct HTTP fetch was blocked by Cloudflare",
                is_error=True,
                metadata={"cloudflare_blocked": True},
            )

    class _FakeHeadless:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            raise AssertionError("headless fallback should not run when headed is available")

    class _FakeHeaded:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="",
                metadata={
                    "extracted_document": {
                        "browser_block_signal": "empty_extraction",
                        "extraction_status": "blocked_or_empty:empty_extraction",
                    }
                },
            )

    monkeypatch.setattr(
        handlers,
        "resolve_fetch_backend",
        lambda *args, **kwargs: _FakePrimary(),  # noqa: ARG005
    )
    monkeypatch.setattr(
        handlers,
        "get_browser_fetch_backend",
        lambda metadata: _FakeHeadless(),
    )
    monkeypatch.setattr(
        handlers,
        "get_headed_browser_fetch_backend",
        lambda metadata: _FakeHeaded(),
    )

    ctx = _FakeContext(
        {
            "web_fetch_backend": "direct",
            "web_fetch_fallback_browser": True,
            "web_browser_fetch_headed_fallback_enabled": True,
        }
    )
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)

    assert result.is_error
    assert "headed browser fallback failed" in result.output.lower()
    assert "empty_extraction" in result.output
    metadata = result.metadata or {}
    assert metadata.get("browser_fallback_modes_attempted") == ["headed"]
    assert metadata.get("browser_fallback_success") is False


@pytest.mark.asyncio
async def test_headed_fallback_skipped_when_manager_does_not_allow_headed() -> None:
    from cognis.tools.executor.web.backends import (
        get_headed_browser_fetch_backend,
        headed_fallback_enabled,
    )

    manager = BrowserManager(enabled=True, headed_allowed=False)
    metadata: dict[str, Any] = {
        BROWSER_MANAGER_KEY: manager,
        "web_browser_fetch_headed_fallback_enabled": True,
    }
    assert headed_fallback_enabled(metadata) is False
    assert get_headed_browser_fetch_backend(metadata) is None


@pytest.mark.asyncio
async def test_browser_fetch_headed_mode_opens_headless_false() -> None:
    manager = _FakeManager(html="<html><body><h1>Hello</h1></body></html>")
    backend = BrowserFetchBackend(manager, headed=True)  # type: ignore[arg-type]

    result = await backend.fetch("https://example.com", output_format="markdown")

    assert not result.is_error
    assert backend.mode == "headed"
    assert backend.headed is True
    assert manager.opened, "expected open_session to be called"
    call = manager.opened[0]
    assert call["headless"] is False
    metadata = result.metadata or {}
    assert metadata.get("browser_fetch_mode") == "headed"
    assert metadata.get("browser_fetch") is True


@pytest.mark.asyncio
async def test_web_fetch_fallback_uses_manager_from_shared_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrowserManager wired in shared_runtime_metadata is found by web_fetch."""
    from cognis.tools.executor.browser.manager import BROWSER_MANAGER_KEY
    from cognis.tools.executor.web import handlers

    class _FakePrimary:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            return ToolResult(
                output="Direct HTTP fetch was blocked by Cloudflare",
                is_error=True,
                metadata={"cloudflare_blocked": True},
            )

    browser_used = False

    class _FakeBrowser:
        async def fetch(self, url: str, **_: Any) -> ToolResult:
            nonlocal browser_used
            browser_used = True
            return ToolResult(output="rendered from browser")

    monkeypatch.setattr(handlers, "resolve_fetch_backend", lambda *a, **k: _FakePrimary())
    monkeypatch.setattr(handlers, "get_browser_fetch_backend", lambda metadata: _FakeBrowser())

    manager = BrowserManager(enabled=True)
    shared = {
        BROWSER_MANAGER_KEY: manager,
        "web_fetch_fallback_browser": True,
    }
    per_call = {
        "web_fetch_backend": "direct",
        "web_fetch_fallback_browser": True,
    }

    ctx = _FakeContext(per_call, shared_runtime_metadata=shared)
    result = await handlers.handle_web_fetch({"url": "https://example.com"}, ctx)

    assert not result.is_error
    assert browser_used is True
