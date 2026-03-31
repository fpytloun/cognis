"""Web tool handlers — fetch, search, crawl, map, research."""

from __future__ import annotations

from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.backends import (
    resolve_fetch_backend,
    resolve_search_backend,
)
from cognis.tools.executor.web.backends.tavily import TavilyBackend
from cognis.tools.registry import ToolExecutionContext


async def handle_web_fetch(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Fetch content from a URL and return it as text or markdown."""
    url = arguments.get("url", "")
    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    output_format = arguments.get("format", "markdown")
    timeout = arguments.get("timeout", 30)
    backend_name = arguments.get("backend")

    # Collect backend-specific options
    options: dict[str, Any] = {}
    for key in ("query", "extract_depth", "chunks_per_source", "include_images"):
        if key in arguments:
            options[key] = arguments[key]

    backend = resolve_fetch_backend(context.runtime_metadata, backend_name)
    return await backend.fetch(
        url,
        output_format=output_format,
        timeout=int(timeout) if timeout else 30,
        options=options if options else None,
    )


async def handle_web_search(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Search the web for information."""
    query = arguments.get("query", "")
    if not query:
        return ToolResult(output="No search query provided.", is_error=True)

    num_results = int(arguments.get("num_results", 8))
    backend_name = arguments.get("backend")

    # Collect backend-specific options
    options: dict[str, Any] = {}
    for key in (
        # Tavily options
        "search_depth",
        "topic",
        "include_answer",
        "include_raw_content",
        "include_images",
        "include_image_descriptions",
        "include_domains",
        "exclude_domains",
        "country",
        "days",
        "time_range",
        "auto_parameters",
        "chunks_per_source",
        # Brave options
        "search_lang",
        "ui_lang",
        "offset",
        "safesearch",
        "freshness",
        "text_decorations",
        "spellcheck",
        "extra_snippets",
        "goggles_id",
        "units",
        "result_filter",
        # Direct/DDG options
        "region",
        "timelimit",
    ):
        if key in arguments:
            options[key] = arguments[key]

    backend = resolve_search_backend(context.runtime_metadata, backend_name)
    return await backend.search(
        query,
        num_results=num_results,
        options=options if options else None,
    )


async def handle_web_crawl(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Crawl a website starting from a URL (requires Tavily backend)."""
    tavily = _require_tavily(context)
    if isinstance(tavily, ToolResult):
        return tavily

    url = arguments.get("url", "")
    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    options: dict[str, Any] = {}
    for key in (
        "max_depth",
        "max_breadth",
        "limit",
        "instructions",
        "select_paths",
        "select_domains",
        "exclude_paths",
        "exclude_domains",
        "allow_external",
        "extract_depth",
        "format",
        "include_images",
    ):
        if key in arguments:
            options[key] = arguments[key]

    return await tavily.crawl(url, options=options if options else None)


async def handle_web_map(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Map a website's structure (requires Tavily backend)."""
    tavily = _require_tavily(context)
    if isinstance(tavily, ToolResult):
        return tavily

    url = arguments.get("url", "")
    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    options: dict[str, Any] = {}
    for key in (
        "max_depth",
        "max_breadth",
        "limit",
        "instructions",
        "select_paths",
        "select_domains",
        "exclude_paths",
        "exclude_domains",
        "allow_external",
        "timeout",
    ):
        if key in arguments:
            options[key] = arguments[key]

    return await tavily.map_site(url, options=options if options else None)


async def handle_web_research(
    arguments: dict[str, Any], context: ToolExecutionContext
) -> ToolResult:
    """Perform deep research on a topic (requires Tavily backend)."""
    tavily = _require_tavily(context)
    if isinstance(tavily, ToolResult):
        return tavily

    query = arguments.get("input", "")
    if not query:
        return ToolResult(output="No research query provided.", is_error=True)

    options: dict[str, Any] = {}
    if "model" in arguments:
        options["model"] = arguments["model"]

    return await tavily.research(query, options=options if options else None)


def _require_tavily(context: ToolExecutionContext) -> TavilyBackend | ToolResult:
    """Get the Tavily backend or return an error ToolResult."""
    from cognis.tools.executor.web.backends import get_tavily_backend

    tavily = get_tavily_backend(context.runtime_metadata)
    if tavily is None:
        return ToolResult(
            output=(
                "This tool requires the Tavily backend. "
                "Configure a Tavily API key in Settings > Secrets "
                "(add a secret named 'tavily_api_key')."
            ),
            is_error=True,
        )
    return tavily
