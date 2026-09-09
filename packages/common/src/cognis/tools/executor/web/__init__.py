"""Web tools — fetch, search, crawl, map, research.

Supports configurable backends: direct (httpx + DDGS metasearch),
Tavily (search, extract, crawl, map, research), and
Brave (web search).
"""

from __future__ import annotations

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
