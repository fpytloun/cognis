"""Web tool backend resolution."""

from __future__ import annotations

from typing import Any

from cognis.tools.executor.web.backends.brave import BraveBackend
from cognis.tools.executor.web.backends.direct import DirectBackend
from cognis.tools.executor.web.backends.protocol import WebFetchBackend, WebSearchBackend
from cognis.tools.executor.web.backends.tavily import TavilyBackend

__all__ = [
    "DirectBackend",
    "TavilyBackend",
    "BraveBackend",
    "WebFetchBackend",
    "WebSearchBackend",
    "resolve_fetch_backend",
    "resolve_search_backend",
]

# Singleton instances — backends are stateless, share across calls.
_direct = DirectBackend()
_tavily: TavilyBackend | None = None
_brave: BraveBackend | None = None


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


def resolve_fetch_backend(
    metadata: dict[str, Any],
    backend_override: str | None = None,
) -> WebFetchBackend:
    """Resolve the fetch backend from runtime metadata and optional override."""
    backend_name = backend_override or metadata.get("web_backend", "direct")
    secrets = metadata.get("web_secrets", {})

    if backend_name == "tavily":
        tavily = _get_tavily(secrets)
        if tavily is not None:
            return tavily
    # Brave has no fetch — fall through to direct.
    return _direct


def resolve_search_backend(
    metadata: dict[str, Any],
    backend_override: str | None = None,
) -> WebSearchBackend:
    """Resolve the search backend from runtime metadata and optional override."""
    backend_name = backend_override or metadata.get("web_backend", "direct")
    secrets = metadata.get("web_secrets", {})

    if backend_name == "tavily":
        tavily = _get_tavily(secrets)
        if tavily is not None:
            return tavily
    if backend_name == "brave":
        brave = _get_brave(secrets)
        if brave is not None:
            return brave
    return _direct


def get_tavily_backend(metadata: dict[str, Any]) -> TavilyBackend | None:
    """Return the Tavily backend if configured, or None."""
    secrets = metadata.get("web_secrets", {})
    return _get_tavily(secrets)


def available_backends(metadata: dict[str, Any]) -> list[str]:
    """Return names of backends that are configured and usable."""
    result = ["direct"]
    secrets = metadata.get("web_secrets", {})
    if secrets.get("tavily_api_key"):
        result.append("tavily")
    if secrets.get("brave_api_key"):
        result.append("brave")
    return result
