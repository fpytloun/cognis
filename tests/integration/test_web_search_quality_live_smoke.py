"""Opt-in live Brave search smoke for previously observed quality failures."""

import os
from urllib.parse import urlparse

import pytest

from cognis.tools.executor.web.backends.brave import BraveBackend

pytestmark = pytest.mark.skipif(
    os.getenv("COGNIS_RUN_WEB_LIVE_SMOKE") != "1" or not os.getenv("BRAVE_SEARCH_API_KEY"),
    reason="set COGNIS_RUN_WEB_LIVE_SMOKE=1 and BRAVE_SEARCH_API_KEY",
)


@pytest.mark.asyncio
async def test_live_czech_domain_search_is_provider_scoped_and_locale_aware() -> None:
    backend = BraveBackend(api_key=os.environ["BRAVE_SEARCH_API_KEY"])
    result = await backend.search(
        "aktuální české zprávy",
        num_results=8,
        options={
            "country": "CZ",
            "include_domains": ["novinky.cz"],
            "time_range": "day",
        },
    )
    metadata = result.metadata or {}
    assert "site:novinky.cz" in str(metadata.get("effective_query"))
    assert metadata.get("locale_fallback") is True
    normalized = metadata.get("normalized_results")
    assert isinstance(normalized, list) and normalized
    assert all(
        (urlparse(str(row.get("url") or "")).hostname or "").endswith("novinky.cz")
        for row in normalized
    )


@pytest.mark.asyncio
async def test_live_world_news_avoids_press_release_as_top_result() -> None:
    backend = BraveBackend(api_key=os.environ["BRAVE_SEARCH_API_KEY"])
    result = await backend.search(
        "top world news",
        num_results=8,
        options={"search_mode": "news", "time_range": "week"},
    )
    normalized = (result.metadata or {}).get("normalized_results")
    assert isinstance(normalized, list) and normalized
    top = f"{normalized[0].get('title')} {normalized[0].get('url')}".lower()
    assert not any(
        marker in top for marker in ("prnewswire", "businesswire", "globenewswire", "press release")
    )


@pytest.mark.asyncio
async def test_live_oriental_cat_search_has_source_diversity() -> None:
    backend = BraveBackend(api_key=os.environ["BRAVE_SEARCH_API_KEY"])
    result = await backend.search(
        "Oriental Shorthair cat temperament health owner experiences",
        num_results=10,
    )
    normalized = (result.metadata or {}).get("normalized_results")
    assert isinstance(normalized, list)
    domains = {
        (urlparse(str(row.get("url") or "")).hostname or "").removeprefix("www.")
        for row in normalized
    }
    assert len(domains) >= 3
