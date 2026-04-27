"""Web tool backend resolution.

Search and fetch backends are resolved independently — each web tool
picks the appropriate backend based on operation. ``web_search`` uses
the ``web_search_backend`` runtime metadata key (default ``direct``);
``web_fetch`` uses ``web_fetch_backend`` (default ``direct``). The legacy
single-axis ``web_backend`` metadata key is honoured as a fallback for
back-compat with executors still rolling out the split.

Per-call ``backend`` overrides supplied by the LLM continue to work for
both axes.
"""

from __future__ import annotations

from typing import Any

from cognis.tools.executor.browser.manager import BROWSER_MANAGER_KEY, BrowserManager
from cognis.tools.executor.web.backends.brave import BraveBackend
from cognis.tools.executor.web.backends.browser import BrowserFetchBackend
from cognis.tools.executor.web.backends.direct import DirectBackend
from cognis.tools.executor.web.backends.protocol import WebFetchBackend, WebSearchBackend
from cognis.tools.executor.web.backends.searxng import SearxngBackend
from cognis.tools.executor.web.backends.tavily import TavilyBackend

__all__ = [
    "BraveBackend",
    "BrowserFetchBackend",
    "DirectBackend",
    "SearxngBackend",
    "TavilyBackend",
    "WebFetchBackend",
    "WebSearchBackend",
    "available_fetch_backends",
    "available_search_backends",
    "available_backends",
    "get_tavily_backend",
    "resolve_fetch_backend",
    "resolve_search_backend",
]

# Singleton instances — backends are stateless w.r.t. their own state but
# hold connection pools and circuit breakers we want to share.
_direct = DirectBackend()
_tavily: TavilyBackend | None = None
_brave: BraveBackend | None = None
_searxng: SearxngBackend | None = None
_browser_fetch: BrowserFetchBackend | None = None
_browser_fetch_manager: BrowserManager | None = None


def _get_tavily(secrets: dict[str, str]) -> TavilyBackend | None:
    global _tavily  # noqa: PLW0603
    api_key = secrets.get("tavily_api_key", "")
    if not api_key:
        return None
    if _tavily is None or _tavily.api_key != api_key:
        _tavily = TavilyBackend(api_key=api_key)
    return _tavily


def _get_brave(secrets: dict[str, str]) -> BraveBackend | None:
    global _brave  # noqa: PLW0603
    api_key = secrets.get("brave_api_key", "")
    if not api_key:
        return None
    if _brave is None or _brave.api_key != api_key:
        _brave = BraveBackend(api_key=api_key)
    return _brave


def _get_searxng(metadata: dict[str, Any]) -> SearxngBackend | None:
    global _searxng  # noqa: PLW0603
    base_url = str(metadata.get("web_searxng_url") or "").strip()
    if not base_url:
        return None
    engines = metadata.get("web_searxng_engines")
    categories = metadata.get("web_searxng_categories")
    language = metadata.get("web_searxng_language")
    if (
        _searxng is None
        or _searxng.base_url != base_url
        or _searxng.default_engines != engines
        or _searxng.default_categories != categories
        or _searxng.default_language != language
    ):
        _searxng = SearxngBackend(
            base_url=base_url,
            engines=engines if isinstance(engines, str) and engines else None,
            categories=categories if isinstance(categories, str) and categories else None,
            language=language if isinstance(language, str) and language else None,
        )
    return _searxng


def _get_browser_fetch(metadata: dict[str, Any]) -> BrowserFetchBackend | None:
    """Construct (and cache) the browser fetch backend bound to this executor."""
    global _browser_fetch, _browser_fetch_manager  # noqa: PLW0603
    manager = metadata.get(BROWSER_MANAGER_KEY)
    if not isinstance(manager, BrowserManager) or not manager.enabled:
        return None
    wait_timeout_raw = metadata.get("web_browser_fetch_wait_timeout_seconds", 30.0)
    idle_raw = metadata.get("web_browser_fetch_session_idle_seconds", 60.0)
    try:
        wait_timeout = float(wait_timeout_raw)
    except (TypeError, ValueError):
        wait_timeout = 30.0
    try:
        idle_timeout = float(idle_raw)
    except (TypeError, ValueError):
        idle_timeout = 60.0
    if (
        _browser_fetch is None
        or _browser_fetch_manager is not manager
        or _browser_fetch._wait_timeout_seconds != wait_timeout  # noqa: SLF001
    ):
        _browser_fetch = BrowserFetchBackend(
            manager,
            wait_timeout_seconds=wait_timeout,
            session_idle_seconds=idle_timeout,
        )
        _browser_fetch_manager = manager
    return _browser_fetch


def _resolve_backend_name(metadata: dict[str, Any], override: str | None, *, axis: str) -> str:
    """Pick a backend name for the requested axis (search/fetch).

    Per-call override always wins. Otherwise prefer the new split key
    (``web_search_backend`` / ``web_fetch_backend``); fall back to the
    legacy ``web_backend`` for executors that have not yet been pushed
    the new metadata. Final fallback is ``direct``.
    """
    if override and isinstance(override, str):
        return override
    split_key = "web_search_backend" if axis == "search" else "web_fetch_backend"
    value = metadata.get(split_key)
    if isinstance(value, str) and value:
        return value
    legacy = metadata.get("web_backend")
    if isinstance(legacy, str) and legacy:
        return legacy
    return "direct"


def resolve_fetch_backend(
    metadata: dict[str, Any],
    backend_override: str | None = None,
) -> WebFetchBackend:
    """Resolve the fetch backend from runtime metadata and optional override."""
    backend_name = _resolve_backend_name(metadata, backend_override, axis="fetch").lower()
    secrets = metadata.get("web_secrets", {})

    if backend_name == "tavily":
        tavily = _get_tavily(secrets)
        if tavily is not None:
            return tavily
    if backend_name == "browser":
        browser = _get_browser_fetch(metadata)
        if browser is not None:
            return browser
    # All other names (direct, brave, searxng) fall through to direct.
    # Brave + SearXNG are search-only; trying to fetch through them is a
    # no-op so we degrade gracefully.
    return _direct


def resolve_search_backend(
    metadata: dict[str, Any],
    backend_override: str | None = None,
) -> WebSearchBackend:
    """Resolve the search backend from runtime metadata and optional override."""
    backend_name = _resolve_backend_name(metadata, backend_override, axis="search").lower()
    secrets = metadata.get("web_secrets", {})

    if backend_name == "tavily":
        tavily = _get_tavily(secrets)
        if tavily is not None:
            return tavily
    if backend_name == "brave":
        brave = _get_brave(secrets)
        if brave is not None:
            return brave
    if backend_name == "searxng":
        searxng = _get_searxng(metadata)
        if searxng is not None:
            return searxng
    return _direct


def get_tavily_backend(metadata: dict[str, Any]) -> TavilyBackend | None:
    """Return the Tavily backend if configured, or None."""
    secrets = metadata.get("web_secrets", {})
    return _get_tavily(secrets)


def get_browser_fetch_backend(metadata: dict[str, Any]) -> BrowserFetchBackend | None:
    """Return the browser fetch backend if a BrowserManager is wired in."""
    return _get_browser_fetch(metadata)


def available_fetch_backends(metadata: dict[str, Any]) -> list[str]:
    """Return names of fetch-capable backends available on this executor."""
    result = ["direct"]
    secrets = metadata.get("web_secrets", {})
    if secrets.get("tavily_api_key"):
        result.append("tavily")
    manager = metadata.get(BROWSER_MANAGER_KEY)
    if isinstance(manager, BrowserManager) and manager.enabled:
        result.append("browser")
    return result


def available_search_backends(metadata: dict[str, Any]) -> list[str]:
    """Return names of search-capable backends available on this executor."""
    result = ["direct"]
    secrets = metadata.get("web_secrets", {})
    if secrets.get("tavily_api_key"):
        result.append("tavily")
    if secrets.get("brave_api_key"):
        result.append("brave")
    if str(metadata.get("web_searxng_url") or "").strip():
        result.append("searxng")
    return result


def available_backends(metadata: dict[str, Any]) -> list[str]:
    """Return the union of search + fetch backends available (legacy shape)."""
    seen: list[str] = []
    for name in available_search_backends(metadata):
        if name not in seen:
            seen.append(name)
    for name in available_fetch_backends(metadata):
        if name not in seen:
            seen.append(name)
    return seen
