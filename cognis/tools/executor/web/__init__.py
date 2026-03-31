"""Web tools — fetch, search, crawl, map, research.

Supports configurable backends: direct (httpx + DuckDuckGo),
Tavily (search, extract, crawl, map, research), and
Brave (web search).
"""

from __future__ import annotations

from cognis.tools.executor.web.handlers import (
    handle_web_crawl,
    handle_web_fetch,
    handle_web_map,
    handle_web_research,
    handle_web_search,
)

__all__ = [
    "handle_web_fetch",
    "handle_web_search",
    "handle_web_crawl",
    "handle_web_map",
    "handle_web_research",
]
