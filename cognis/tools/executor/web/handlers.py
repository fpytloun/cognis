"""Web tool handlers — fetch, search, crawl, map, research."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.web.backends import (
    resolve_fetch_backend,
    resolve_search_backend,
)
from cognis.tools.executor.web.backends.tavily import TavilyBackend
from cognis.tools.registry import ToolExecutionContext

_TAVILY_SEARCH_DEPTHS = {"basic", "advanced", "fast", "ultra-fast"}
_TAVILY_TOPICS = {"general", "news", "finance"}
_TAVILY_TIME_RANGES = {"day", "week", "month", "year", "d", "w", "m", "y"}
_TAVILY_ANSWER_MODES = {"basic", "advanced"}
_TAVILY_RAW_CONTENT_MODES = {"markdown", "text"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_optional_web_value(value: Any) -> Any:
    """Normalize optional tool values before forwarding them to web backends.

    LLMs often materialize optional parameters as empty strings or empty
    collections instead of omitting them. Many downstream APIs treat those as
    invalid present values rather than as "unset". Preserve explicit booleans
    (including ``False``), numeric zeros, and any non-empty values.
    """
    if isinstance(value, str) and value == "":
        return None
    if isinstance(value, (list, dict)) and len(value) == 0:
        return None
    return value


def _collect_optional_options(arguments: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Collect optional backend options, omitting empty optional values."""
    options: dict[str, Any] = {}
    for key in keys:
        if key not in arguments:
            continue
        value = _normalize_optional_web_value(arguments[key])
        if value is None:
            continue
        options[key] = value
    return options


def _selected_search_backend(arguments: dict[str, Any], context: ToolExecutionContext) -> str:
    backend = arguments.get("backend")
    if isinstance(backend, str) and backend:
        return backend
    configured = context.runtime_metadata.get("web_backend", "direct")
    return configured if isinstance(configured, str) and configured else "direct"


def _normalize_tavily_string_option(
    options: dict[str, Any],
    key: str,
    *,
    allowed: set[str],
) -> str | None:
    value = options.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Tavily option '{key}' must be a string.")
    normalized = value.strip().lower()
    if normalized not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"Tavily option '{key}' must be one of: {allowed_values}.")
    options[key] = normalized
    return normalized


def _normalize_tavily_search_options(options: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(options)
    search_depth = _normalize_tavily_string_option(
        normalized,
        "search_depth",
        allowed=_TAVILY_SEARCH_DEPTHS,
    )
    topic = _normalize_tavily_string_option(normalized, "topic", allowed=_TAVILY_TOPICS)
    _normalize_tavily_string_option(normalized, "time_range", allowed=_TAVILY_TIME_RANGES)

    include_answer = normalized.get("include_answer")
    if isinstance(include_answer, str):
        mode = include_answer.strip().lower()
        if mode not in _TAVILY_ANSWER_MODES:
            raise ValueError(
                "Tavily option 'include_answer' must be true, false, 'basic', or 'advanced'."
            )
        normalized["include_answer"] = mode

    include_raw_content = normalized.get("include_raw_content")
    if isinstance(include_raw_content, str):
        mode = include_raw_content.strip().lower()
        if mode not in _TAVILY_RAW_CONTENT_MODES:
            raise ValueError(
                "Tavily option 'include_raw_content' must be true, false, 'markdown', or 'text'."
            )
        normalized["include_raw_content"] = mode

    for date_key in ("start_date", "end_date"):
        value = normalized.get(date_key)
        if value is None:
            continue
        if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value.strip()):
            raise ValueError(f"Tavily option '{date_key}' must use YYYY-MM-DD format.")
        stripped = value.strip()
        try:
            date.fromisoformat(stripped)
        except ValueError as exc:
            raise ValueError(f"Tavily option '{date_key}' must be a real calendar date.") from exc
        normalized[date_key] = stripped

    chunks_per_source = normalized.get("chunks_per_source")
    if chunks_per_source is not None:
        try:
            chunks = int(chunks_per_source)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Tavily option 'chunks_per_source' must be an integer between 1 and 3."
            ) from exc
        if chunks < 1 or chunks > 3:
            raise ValueError("Tavily option 'chunks_per_source' must be between 1 and 3.")
        if search_depth != "advanced":
            normalized.pop("chunks_per_source", None)
        else:
            normalized["chunks_per_source"] = chunks

    country = normalized.get("country")
    if country is not None:
        if topic != "general":
            normalized.pop("country", None)
        elif isinstance(country, str):
            normalized["country"] = country.strip().lower()
        else:
            raise ValueError("Tavily option 'country' must be a country name string.")

    return normalized


async def handle_web_fetch(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Fetch content from a URL and return it as text or markdown."""
    url = arguments.get("url", "")
    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    output_format = arguments.get("format", "markdown")
    timeout = arguments.get("timeout", 30)
    backend_name = arguments.get("backend")

    options = _collect_optional_options(
        arguments,
        ("query", "extract_depth", "chunks_per_source", "include_images"),
    )

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

    options = _collect_optional_options(
        arguments,
        (
            # Tavily options
            "search_depth",
            "topic",
            "include_answer",
            "include_raw_content",
            "include_images",
            "include_image_descriptions",
            "include_favicon",
            "include_domains",
            "exclude_domains",
            "country",
            "days",
            "time_range",
            "start_date",
            "end_date",
            "auto_parameters",
            "chunks_per_source",
            "exact_match",
            "include_usage",
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
        ),
    )

    if _selected_search_backend(arguments, context) == "tavily":
        try:
            options = _normalize_tavily_search_options(options)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)

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

    options = _collect_optional_options(
        arguments,
        (
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
        ),
    )

    return await tavily.crawl(url, options=options if options else None)


async def handle_web_map(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Map a website's structure (requires Tavily backend)."""
    tavily = _require_tavily(context)
    if isinstance(tavily, ToolResult):
        return tavily

    url = arguments.get("url", "")
    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    options = _collect_optional_options(
        arguments,
        (
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
        ),
    )

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

    options = _collect_optional_options(arguments, ("model",))

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
