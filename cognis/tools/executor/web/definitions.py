"""Dynamic web tool definitions based on available backends.

Web tool schemas are generated at runtime so the LLM only sees
backends and parameters that are actually configured. This prevents
the LLM from attempting to use unavailable backends or backend-specific
parameters.
"""

from __future__ import annotations

from cognis.models.tool import ToolDefinition, ToolSource

_EXECUTOR_SOURCE = ToolSource(type="executor")


def web_tool_definitions(
    available_backends: list[str],
    *,
    default_backend: str | None = None,
) -> list[ToolDefinition]:
    """Generate web tool definitions based on configured backends.

    Args:
        available_backends: List of backend names that are configured
            and usable (e.g. ``["direct"]`` or ``["direct", "tavily", "brave"]``).

    Returns:
        Tool definitions with schemas tailored to the available backends.
    """
    has_tavily = "tavily" in available_backends
    has_brave = "brave" in available_backends
    has_multiple = len(available_backends) > 1
    resolved_default_backend = default_backend or available_backends[0]
    if resolved_default_backend not in available_backends:
        resolved_default_backend = available_backends[0]

    fetch_default_backend = _resolve_capable_default_backend(
        resolved_default_backend,
        [backend for backend in available_backends if backend != "brave"],
    )

    tools: list[ToolDefinition] = [
        _build_web_fetch(
            available_backends,
            has_tavily,
            has_multiple,
            default_backend=fetch_default_backend,
        ),
        _build_web_search(
            available_backends,
            has_tavily,
            has_brave,
            has_multiple,
            default_backend=resolved_default_backend,
        ),
    ]

    if has_tavily:
        tools.extend(
            [
                _build_web_crawl(),
                _build_web_map(),
                _build_web_research(),
            ]
        )

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
    backends: list[str],
    has_tavily: bool,
    has_multiple: bool,
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
    }

    if has_multiple:
        fetch_backends = [b for b in backends if b != "brave"]  # brave has no fetch
        properties["backend"] = {
            "type": "string",
            "enum": fetch_backends,
            "description": (
                f"Backend to use (default: {default_backend}). "
                "Omit this unless you need to override the configured default. "
                f"{_fetch_backend_hints(fetch_backends)}"
            ),
        }

    desc = "Fetch content from a URL and return it as text or markdown."
    if has_tavily:
        desc += (
            f" The configured default backend is '{default_backend}'. "
            "Use 'tavily' for higher-quality extraction with content reranking when you need to override it."
        )

    return ToolDefinition(
        name="web_fetch",
        description=desc,
        parameters={"type": "object", "properties": properties, "required": ["url"]},
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        timeout_seconds=60,
    )


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


def _build_web_search(
    backends: list[str],
    has_tavily: bool,
    has_brave: bool,
    has_multiple: bool,
    *,
    default_backend: str,
) -> ToolDefinition:
    properties: dict[str, object] = {
        "query": {"type": "string", "description": "Search query"},
        "num_results": {
            "type": "integer",
            "description": "Number of results (default: 8, max varies by backend)",
        },
    }

    if has_multiple:
        properties["backend"] = {
            "type": "string",
            "enum": backends,
            "description": (
                f"Backend to use (default: {default_backend}). "
                "Omit this unless you need to override the configured default. "
                f"{_search_backend_hints(backends)}"
            ),
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
        properties["include_images"] = {
            "type": "boolean",
            "description": "Tavily: include query-related and per-result images",
        }
        properties["include_image_descriptions"] = {
            "type": "boolean",
            "description": "Tavily: include image descriptions when images are returned",
        }
        properties["include_domains"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tavily: prefer these domains instead of using site: operators",
        }
        properties["exclude_domains"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tavily: exclude these domains from search results",
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
        properties["time_range"] = {
            "type": "string",
            "description": (
                "Recency filter. Tavily: 'day','week','month','year'. "
                "Brave: 'pd','pw','pm','py' or 'YYYY-MM-DDtoYYYY-MM-DD'."
                if has_tavily and has_brave
                else "Tavily: recency filter — 'day','week','month','year'."
                if has_tavily
                else "Brave: freshness — 'pd','pw','pm','py' or date range."
            ),
        }
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

    desc = _search_description(backends, has_tavily, has_brave)

    return ToolDefinition(
        name="web_search",
        description=desc,
        parameters={"type": "object", "properties": properties, "required": ["query"]},
        source=_EXECUTOR_SOURCE,
        category="web",
        read_only=True,
        timeout_seconds=60,
    )


# ---------------------------------------------------------------------------
# Tavily-only tools
# ---------------------------------------------------------------------------


def _build_web_crawl() -> ToolDefinition:
    return ToolDefinition(
        name="web_crawl",
        description=(
            "Crawl a website starting from a URL. Extracts content from pages "
            "with configurable depth and breadth."
        ),
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
        timeout_seconds=120,
    )


def _build_web_map() -> ToolDefinition:
    return ToolDefinition(
        name="web_map",
        description=(
            "Map a website's structure. Returns a list of URLs found starting "
            "from the base URL. Useful for discovering site structure before crawling."
        ),
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
        timeout_seconds=300,
    )


# ---------------------------------------------------------------------------
# Description helpers
# ---------------------------------------------------------------------------


def _search_description(backends: list[str], has_tavily: bool, has_brave: bool) -> str:
    if not has_tavily and not has_brave:
        return "Search the web using DuckDuckGo. Free, no API key needed."

    parts = ["Search the web for information. Backends:"]
    if "direct" in backends:
        parts.append("'direct' (DuckDuckGo, free)")
    if has_tavily:
        parts.append("'tavily' (AI-optimized, supports answer generation)")
    if has_brave:
        parts.append("'brave' (large index, freshness filters)")
    description = " ".join(parts) + "."
    if has_tavily:
        description += (
            " For Tavily, prefer include_domains/exclude_domains over site: operators in the query."
            " Keep the query focused on the subject itself rather than search syntax."
            " For exact identifiers, prefer shorter queries and use exact_match when appropriate."
            " Use country only with topic='general'."
        )
    return description


def _fetch_backend_hints(backends: list[str]) -> str:
    hints = []
    for b in backends:
        if b == "direct":
            hints.append("'direct': free httpx fetch")
        elif b == "tavily":
            hints.append("'tavily': higher quality extraction")
    return ", ".join(hints) + "." if hints else ""


def _search_backend_hints(backends: list[str]) -> str:
    hints = []
    for b in backends:
        if b == "direct":
            hints.append("'direct': DuckDuckGo (free)")
        elif b == "tavily":
            hints.append("'tavily': AI-optimized search")
        elif b == "brave":
            hints.append("'brave': large web index")
    return ", ".join(hints) + "." if hints else ""
