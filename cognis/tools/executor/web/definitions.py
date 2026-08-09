"""Dynamic web tool definitions based on available backends.

Web tool schemas are generated at runtime so the LLM only sees
backends and parameters that are actually configured. This prevents
the LLM from attempting to use unavailable backends or backend-specific
parameters.
"""

from __future__ import annotations

from cognis.models.tool import NativeToolDefinition as ToolDefinition
from cognis.models.tool import ToolSource

_EXECUTOR_SOURCE = ToolSource(type="executor")


def web_tool_definitions(
    available_backends: list[str],
    *,
    default_backend: str | None = None,
    available_search_backends: list[str] | None = None,
    available_fetch_backends: list[str] | None = None,
    default_search_backend: str | None = None,
    default_fetch_backend: str | None = None,
) -> list[ToolDefinition]:
    """Generate web tool definitions based on configured backends.

    Search and fetch backends are selected independently. The legacy
    ``available_backends`` list is honoured when the per-axis lists are
    not supplied (existing callers).

    Args:
        available_backends: Union of search + fetch backends (legacy).
        available_search_backends: Search-capable backends only.
        available_fetch_backends: Fetch-capable backends only.
        default_backend: Legacy single-axis default.
        default_search_backend: Default for ``web_search``.
        default_fetch_backend: Default for ``web_fetch``.
    """
    search_backends = (
        available_search_backends or [b for b in available_backends if b != "browser"] or ["direct"]
    )
    fetch_backends = (
        available_fetch_backends
        or [b for b in available_backends if b != "brave" and b != "searxng"]
        or ["direct"]
    )

    has_tavily_search = "tavily" in search_backends
    has_tavily_fetch = "tavily" in fetch_backends
    has_browser_fetch = "browser" in fetch_backends

    legacy_default = default_backend or available_backends[0] if available_backends else "direct"
    if legacy_default not in available_backends and available_backends:
        legacy_default = available_backends[0]

    resolved_search_default = (
        default_search_backend
        or (legacy_default if legacy_default in search_backends else None)
        or search_backends[0]
    )
    resolved_fetch_default = (
        default_fetch_backend
        or (legacy_default if legacy_default in fetch_backends else None)
        or fetch_backends[0]
    )

    tools: list[ToolDefinition] = [
        _build_web_fetch(
            has_browser_fetch,
            default_backend=resolved_fetch_default,
        ),
        _build_web_search(
            resolved_search_default == "tavily",
            resolved_search_default == "brave",
            resolved_search_default == "searxng",
            default_backend=resolved_search_default,
        ),
        # Crawl + map are always available — implementation auto-selects
        # the Tavily-native engine when fetch_backend=tavily, otherwise the
        # in-tree DIY orchestrator is used.
        _build_web_crawl(has_tavily_fetch),
        _build_web_map(has_tavily_fetch),
    ]

    # web_research stays Tavily-only by design: the agent loop is the
    # default research path on top of web_search + web_fetch.
    if has_tavily_search or has_tavily_fetch:
        tools.append(_build_web_research())

    return tools


def _resolve_capable_default_backend(default_backend: str, supported_backends: list[str]) -> str:
    """Return the effective default backend for a tool with limited backend support."""
    if default_backend in supported_backends:
        return default_backend
    return supported_backends[0]


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------


def _build_web_fetch(
    has_browser: bool,
    *,
    default_backend: str,
) -> ToolDefinition:
    properties: dict[str, object] = {
        "url": {"type": "string", "description": "URL to fetch"},
        "format": {
            "type": "string",
            "enum": ["text", "markdown", "html"],
            "description": "Output format (default: markdown)",
        },
        "timeout": {"type": "integer", "description": "Timeout in seconds (max 120)"},
        "include_media": {
            "type": "string",
            "enum": ["none", "metadata"],
            "description": "Direct/browser: return extracted media references such as hero and inline images (default: metadata)",
        },
        "media_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 50,
            "description": "Maximum media references to include (default: 10, max 50)",
        },
    }

    desc = (
        "Fetch content from a URL and return it as text or markdown. "
        f"Uses the configured {default_backend} fetch backend. "
        "Direct fetches automatically retry through a headed browser when "
        "available, otherwise through a headless browser, when the controller "
        "is configured to do so."
    )
    extras: list[str] = []
    if has_browser:
        extras.append("Browser rendering is selected automatically by the fallback policy.")
    if extras:
        desc += " " + " ".join(extras)

    return ToolDefinition(
        name="web_fetch",
        description=desc,
        parameters={"type": "object", "properties": properties, "required": ["url"]},
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        content_trust="untrusted",
        timeout_seconds=60,
    )


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


def _build_web_search(
    has_tavily: bool,
    has_brave: bool,
    has_searxng: bool,
    *,
    default_backend: str,
) -> ToolDefinition:
    properties: dict[str, object] = {
        "query": {"type": "string", "description": "Search query"},
        "num_results": {
            "type": "integer",
            "description": "Number of results (default: 8, max varies by backend)",
        },
        "include_images": {
            "type": "boolean",
            "description": (
                "Include image references usable as lazy artifacts in rich deliverables. "
                "Direct uses DuckDuckGo Image Search; Tavily returns query-related images."
            ),
        },
        "image_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 50,
            "description": "Maximum image references to return when include_images is enabled (default: 10, max 50)",
        },
        "include_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Prefer results from these domains",
        },
        "exclude_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exclude results from these domains",
        },
        "time_range": {
            "type": "string",
            "enum": ["any", "day", "week", "month", "year"],
            "description": (
                "Provider-neutral freshness window. Use 'any' for stable resources, "
                "exact identifiers, repositories, papers, videos, and documents "
                "(default: any)."
            ),
        },
        "search_mode": {
            "type": "string",
            "enum": ["web", "news", "images", "videos"],
            "description": (
                "Search vertical. Images and videos request focused media results; "
                "include_images instead adds auxiliary images to another mode."
            ),
        },
        "result_type": {
            "type": "string",
            "enum": ["paper", "repository", "discussion", "document"],
            "description": "Optional preferred content shape within the selected search mode.",
        },
    }

    # Tavily-specific params
    if has_tavily:
        properties["search_depth"] = {
            "type": "string",
            "enum": ["basic", "advanced", "fast", "ultra-fast"],
            "description": "Tavily: search depth (default: basic)",
        }
        properties["topic"] = {
            "type": "string",
            "enum": ["general", "news", "finance"],
            "description": "Tavily: topic category (default: general)",
        }
        properties["include_answer"] = {
            "oneOf": [
                {"type": "boolean"},
                {"type": "string", "enum": ["basic", "advanced"]},
            ],
            "description": "Tavily: generate LLM answer from results",
        }
        properties["include_raw_content"] = {
            "oneOf": [
                {"type": "boolean"},
                {"type": "string", "enum": ["markdown", "text"]},
            ],
            "description": "Tavily: include cleaned raw page content",
        }
        properties["include_image_descriptions"] = {
            "type": "boolean",
            "description": "Tavily: include image descriptions when images are returned",
        }
        properties["chunks_per_source"] = {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "description": "Tavily: max relevant snippets per source (advanced depth only)",
        }
        properties["auto_parameters"] = {
            "type": "boolean",
            "description": "Tavily: automatically tune search parameters from the query",
        }
        properties["start_date"] = {
            "type": "string",
            "description": "Tavily: include results after this date (YYYY-MM-DD)",
        }
        properties["end_date"] = {
            "type": "string",
            "description": "Tavily: include results before this date (YYYY-MM-DD)",
        }
        properties["exact_match"] = {
            "type": "boolean",
            "description": "Tavily: enforce exact quoted phrase matches in the query",
        }
        properties["include_usage"] = {
            "type": "boolean",
            "description": "Tavily: include search credit usage in the response",
        }
        properties["include_favicon"] = {
            "type": "boolean",
            "description": "Tavily: include favicon URLs for results",
        }

    # Brave-specific params
    if has_brave:
        properties["freshness"] = {
            "type": "string",
            "description": "Brave: freshness filter — 'pd','pw','pm','py' or date range",
        }
        properties["extra_snippets"] = {
            "type": "boolean",
            "description": "Brave: include up to 5 extra text excerpts per result",
        }
        properties["safesearch"] = {
            "type": "string",
            "enum": ["off", "moderate", "strict"],
            "description": "Brave: safe search filter (default: moderate)",
        }

    # Shared params (appear when any non-direct backend is available)
    if has_tavily or has_brave:
        properties["country"] = {
            "type": "string",
            "description": (
                "Country filter (Tavily: full name, Brave: 2-letter code)"
                if has_tavily and has_brave
                else "Tavily: boost results from country (full name)"
                if has_tavily
                else "Brave: 2-letter country code"
            ),
        }

    desc = _search_description(
        default_backend=default_backend,
    )

    return ToolDefinition(
        name="web_search",
        description=desc,
        parameters={"type": "object", "properties": properties, "required": ["query"]},
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        content_trust="untrusted",
        timeout_seconds=60,
    )


# ---------------------------------------------------------------------------
# Tavily-only tools
# ---------------------------------------------------------------------------


def _build_web_crawl(has_tavily_fetch: bool) -> ToolDefinition:
    desc = (
        "Crawl a website starting from a URL. Extracts content from pages "
        "with configurable depth and breadth."
    )
    if has_tavily_fetch:
        desc += " Uses Tavily's native crawler when fetch_backend=tavily; otherwise the in-tree DIY orchestrator."
    return ToolDefinition(
        name="web_crawl",
        description=desc,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Root URL to begin crawl"},
                "max_depth": {
                    "type": "integer",
                    "description": "How deep to crawl (1-5, default: 1)",
                },
                "max_breadth": {
                    "type": "integer",
                    "description": "Max links per level (1-500, default: 20)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Total pages to process (default: 50)",
                },
                "instructions": {
                    "type": "string",
                    "description": "Natural language instructions for the crawler",
                },
                "extract_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Extraction depth (default: basic)",
                },
            },
            "required": ["url"],
        },
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        content_trust="untrusted",
        timeout_seconds=120,
    )


def _build_web_map(has_tavily_fetch: bool) -> ToolDefinition:
    desc = (
        "Map a website's structure. Returns a list of URLs found starting "
        "from the base URL. Useful for discovering site structure before crawling."
    )
    if has_tavily_fetch:
        desc += " Uses Tavily's native mapper when fetch_backend=tavily; otherwise sitemap.xml + first-page link enumeration."
    return ToolDefinition(
        name="web_map",
        description=desc,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Root URL to map"},
                "max_depth": {
                    "type": "integer",
                    "description": "Mapping depth (1-5, default: 1)",
                },
                "max_breadth": {
                    "type": "integer",
                    "description": "Links per level (1-500, default: 20)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Total pages to map (default: 50)",
                },
                "instructions": {
                    "type": "string",
                    "description": "Natural language instructions for the mapper",
                },
            },
            "required": ["url"],
        },
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        content_trust="untrusted",
        timeout_seconds=150,
    )


def _build_web_research() -> ToolDefinition:
    return ToolDefinition(
        name="web_research",
        description=(
            "Perform comprehensive research on a topic. Uses multiple searches "
            "and source analysis to produce a detailed report."
        ),
        parameters={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Research task description"},
                "model": {
                    "type": "string",
                    "enum": ["mini", "pro", "auto"],
                    "description": (
                        "Research depth: 'mini' for narrow tasks, 'pro' for broad, "
                        "'auto' selects automatically (default: auto)"
                    ),
                },
            },
            "required": ["input"],
        },
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        content_trust="untrusted",
        timeout_seconds=300,
    )


# ---------------------------------------------------------------------------
# Description helpers
# ---------------------------------------------------------------------------


def _search_description(
    *,
    default_backend: str,
) -> str:
    descriptions = {
        "direct": "DuckDuckGo",
        "tavily": "Tavily",
        "brave": "Brave Search",
        "searxng": "the configured self-hosted SearXNG instance",
    }
    backend_label = descriptions.get(default_backend, default_backend)
    description = f"Search the web for information using {backend_label}."
    if default_backend == "tavily":
        description += (
            " For Tavily, prefer include_domains/exclude_domains over site: operators in the query."
            " Keep the query focused on the subject itself rather than search syntax."
            " For exact identifiers, prefer shorter queries and use exact_match when appropriate."
            " Use country only with topic='general'."
        )
    return description
