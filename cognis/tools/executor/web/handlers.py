"""Web tool handlers — fetch, search, crawl, map, research."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from cognis.models.tool import ToolResult
from cognis.tools.executor.browser.manager import BROWSER_MANAGER_KEY
from cognis.tools.executor.web.backends import (
    get_browser_fetch_backend,
    get_headed_browser_fetch_backend,
    headed_fallback_enabled,
    resolve_fetch_backend,
    resolve_search_backend,
)
from cognis.tools.executor.web.backends.formatting import build_fetch_tool_result
from cognis.tools.executor.web.backends.tavily import TavilyBackend
from cognis.tools.executor.web.concurrency import (
    WebConcurrencyController,
    get_or_create_controller,
    host_for,
)
from cognis.tools.registry import ToolExecutionContext

logger = logging.getLogger(__name__)

# Heuristic markers that indicate the direct backend hit a wall the
# headless browser fallback can usually clear.
_BROWSER_FALLBACK_HINT_TOKENS: tuple[str, ...] = (
    "cloudflare",
    "circuit breaker",
    "http 401",
    "rate limited (http 429)",
    "http 403",
    "http 503",
    "http 502",
    "request failed",
    "request timed out",
)

_TAVILY_SEARCH_DEPTHS = {"basic", "advanced", "fast", "ultra-fast"}
_TAVILY_TOPICS = {"general", "news", "finance"}
_TAVILY_TIME_RANGES = {"day", "week", "month", "year", "d", "w", "m", "y"}
_TAVILY_ANSWER_MODES = {"basic", "advanced"}
_TAVILY_RAW_CONTENT_MODES = {"markdown", "text"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SITE_FILTER_RE = re.compile(
    r"(?P<prefix>^|\s)site:(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?=\s|$)"
)
_BOOLEAN_TOKEN_RE = re.compile(r"\b(?:AND|OR|NOT)\b", re.IGNORECASE)
_EXACT_LOOKUP_FILLER = frozenset({"quote", "quotes", "price", "prices", "ticker", "symbol"})


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
    configured = context.runtime_metadata.get("web_search_backend") or (
        context.runtime_metadata.get("web_backend", "direct")
    )
    return configured if isinstance(configured, str) and configured else "direct"


def _backend_label(backend: Any, default: str = "direct") -> str:
    """Return a short identifier for a resolved backend object."""
    if backend is None:
        return default
    name = type(backend).__name__.lower()
    if "tavily" in name:
        return "tavily"
    if "brave" in name:
        return "brave"
    if "searxng" in name:
        return "searxng"
    if "browser" in name:
        return "browser"
    return default


def _result_is_browser_fallback_candidate(result: ToolResult) -> bool:
    """Return True when ``result`` looks like a transient/blocked failure
    that the headless browser fallback can usually overcome."""
    if not result.is_error:
        return False
    metadata = result.metadata or {}
    if metadata.get("cloudflare_blocked"):
        return True
    output = (result.output or "").lower()
    return any(token in output for token in _BROWSER_FALLBACK_HINT_TOKENS)


def _browser_block_signal(result: ToolResult) -> str | None:
    metadata = result.metadata or {}
    document = metadata.get("extracted_document")
    if not isinstance(document, dict):
        return None
    signal = document.get("browser_block_signal")
    return str(signal) if isinstance(signal, str) and signal else None


def _annotate_fallback_metadata(
    result: ToolResult,
    *,
    fallback_used: bool,
    fallback_mode: str | None,
    modes_attempted: list[str],
    primary_backend: str,
) -> ToolResult:
    if not (fallback_used or modes_attempted):
        return result
    merged = dict(result.metadata or {})
    if fallback_used:
        merged["browser_fallback"] = True
        merged["browser_fallback_attempted"] = True
    if fallback_mode:
        merged["browser_fallback_mode"] = fallback_mode
    if modes_attempted:
        merged["browser_fallback_modes_attempted"] = list(modes_attempted)
    merged.setdefault("primary_backend", primary_backend)
    result.metadata = merged
    return result


def _should_attempt_browser_fallback(
    *,
    result: ToolResult,
    primary_backend_name: str,
    runtime_metadata: dict[str, Any],
    user_override: str | None,
) -> bool:
    """Decide if we should retry a failed fetch through the browser backend.

    Skips when:
    * caller forced a non-direct backend,
    * fallback is disabled via ``web.fetch_fallback_browser`` setting,
    * we're already on the browser backend,
    * the failure is not the kind a browser would fix,
    * no BrowserManager is available on this executor.
    """
    if user_override and user_override.lower() not in {"", "direct"}:
        return False
    if primary_backend_name == "browser":
        return False
    if not _result_is_browser_fallback_candidate(result):
        return False
    fallback_setting = runtime_metadata.get("web_fetch_fallback_browser", True)
    if fallback_setting is False:
        return False
    return get_browser_fetch_backend(runtime_metadata) is not None


def _explain_skipped_fallback(
    *,
    primary_label: str,
    primary_result: ToolResult,
    runtime_metadata: dict[str, Any],
    user_override: str | None,
) -> ToolResult:
    """Annotate primary errors when no browser fallback was attempted.

    The LLM (and user) need to know whether a browser retry happened, was
    blocked, or was disabled — otherwise the new direct-fetch error message
    keeps suggesting fallback that never runs.
    """
    metadata = dict(primary_result.metadata or {})
    metadata.setdefault("primary_backend", primary_label)
    metadata["browser_fallback_attempted"] = False
    fallback_disabled = runtime_metadata.get("web_fetch_fallback_browser", True) is False
    explicit_non_direct = bool(
        user_override
        and isinstance(user_override, str)
        and user_override.lower() not in {"", "direct"}
    )
    suffix: str | None = None
    if explicit_non_direct:
        metadata["browser_fallback_skipped_reason"] = "explicit_backend_override"
    elif primary_label == "browser":
        metadata["browser_fallback_skipped_reason"] = "primary_is_browser"
    elif fallback_disabled:
        metadata["browser_fallback_skipped_reason"] = "fallback_disabled"
        suffix = (
            "Browser fallback is disabled (web.fetch_fallback_browser=false); "
            "no headless or headed retry was attempted."
        )
    elif get_browser_fetch_backend(runtime_metadata) is None:
        from cognis.tools.executor.browser.manager import BrowserManager

        manager = runtime_metadata.get(BROWSER_MANAGER_KEY)
        if isinstance(manager, BrowserManager) and not manager.enabled:
            metadata["browser_fallback_skipped_reason"] = "browser_disabled"
            suffix = (
                "Browser fallback is enabled in web settings but browser tools are "
                "disabled on this executor (browser.enabled=false)."
            )
        else:
            metadata["browser_fallback_skipped_reason"] = "browser_unavailable"
            suffix = (
                "Browser fallback is enabled but no browser runtime is configured on "
                "this executor. Enable browser tools in the executor settings."
            )
    else:
        # Failure profile didn't look browser-fixable (e.g. plain 404).
        metadata["browser_fallback_skipped_reason"] = "not_browser_fixable"
    if suffix:
        primary_result = ToolResult(
            output=f"{primary_result.output.rstrip()}\n{suffix}",
            is_error=True,
            metadata=metadata,
        )
    else:
        primary_result.metadata = metadata
    return primary_result


def _combined_fallback_failure(
    *,
    primary_label: str,
    primary_result: ToolResult,
    fallback_results: list[tuple[str, ToolResult]],
    modes_attempted: list[str],
    headed_skipped_reason: str | None,
) -> ToolResult:
    """Build a combined diagnostic when every fallback mode fails."""
    lines: list[str] = [
        f"{primary_label} fetch failed: {primary_result.output.strip() or 'unknown error'}",
    ]
    for mode, result in fallback_results:
        suffix = result.output.strip() or "unknown error"
        lines.append(f"{mode.capitalize()} browser fallback failed: {suffix}")
    if headed_skipped_reason:
        lines.append(headed_skipped_reason)
    metadata: dict[str, Any] = {
        "primary_backend": primary_label,
        "primary_error": primary_result.output[:500],
        "browser_fallback_attempted": bool(fallback_results),
        "browser_fallback_modes_attempted": list(modes_attempted),
        "browser_fallback_success": False,
    }
    for mode, result in fallback_results:
        if result.metadata:
            metadata.setdefault(f"{mode}_fallback_metadata", dict(result.metadata))
        metadata[f"{mode}_fallback_error"] = (result.output or "")[:500]
    if primary_result.metadata:
        for key in ("cloudflare_blocked", "direct_fetch_blocked"):
            if primary_result.metadata.get(key):
                metadata[key] = True
    return ToolResult(
        output="\n".join(lines),
        is_error=True,
        metadata=metadata,
    )


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

    for domains_key in ("include_domains", "exclude_domains"):
        domains_value = normalized.get(domains_key)
        if domains_value is None:
            continue
        if isinstance(domains_value, str):
            stripped = domains_value.strip()
            normalized[domains_key] = [stripped] if stripped else []
            continue
        if not isinstance(domains_value, list) or not all(
            isinstance(item, str) and item.strip() for item in domains_value
        ):
            raise ValueError(f"Tavily option '{domains_key}' must be a string or list of strings.")
        normalized[domains_key] = [item.strip() for item in domains_value]

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


def _normalize_tavily_query(
    query: str,
    options: dict[str, Any],
) -> tuple[str, dict[str, Any], bool]:
    """Normalize Tavily queries by lifting simple site filters into options."""
    normalized_query = query.strip()
    normalized_options = dict(options)
    include_domains = list(normalized_options.get("include_domains") or [])
    leading_domains, remaining_query = _extract_leading_site_filters(normalized_query)
    if not leading_domains:
        return normalized_query, normalized_options, False
    normalized_query = remaining_query or query.strip()

    changed = False
    if leading_domains:
        merged_domains: list[str] = []
        for domain in [*include_domains, *leading_domains]:
            lowered = str(domain).strip().lower()
            if lowered and lowered not in merged_domains:
                merged_domains.append(lowered)
        normalized_options["include_domains"] = merged_domains
        changed = True

    if normalized_query != query.strip():
        changed = True

    return normalized_query or query.strip(), normalized_options, changed


def _extract_leading_site_filters(query: str) -> tuple[list[str], str]:
    """Extract a leading `site:domain OR site:domain` cluster conservatively."""
    tokens = [token for token in re.split(r"\s+", query.strip()) if token]
    if not tokens:
        return [], query.strip()

    extracted: list[str] = []
    index = 0
    expecting_site = False
    while index < len(tokens):
        site_match = _SITE_FILTER_RE.fullmatch(tokens[index])
        if not site_match:
            break
        extracted.append(site_match.group("domain").lower().strip("."))
        expecting_site = False
        index += 1
        if index >= len(tokens):
            break
        boolean = tokens[index]
        if boolean.upper() != "OR":
            break
        expecting_site = True
        index += 1

    if not extracted:
        return [], query.strip()
    if expecting_site:
        return [], query.strip()
    if index > len(tokens):
        return [], query.strip()
    if index < len(tokens) and _BOOLEAN_TOKEN_RE.fullmatch(tokens[index]):
        return [], query.strip()

    remaining = " ".join(tokens[index:]).strip()
    return extracted, remaining


def _identifier_like_tokens(query: str) -> list[str]:
    tokens = [token for token in re.split(r"\s+", query.strip()) if token]
    result: list[str] = []
    for token in tokens:
        stripped = token.strip(",.;:()[]{}\"'")
        if not stripped:
            continue
        if any(char in stripped for char in ("=", "/", ":", "_")):
            result.append(stripped)
            continue
        if any(char.isdigit() for char in stripped):
            result.append(stripped)
            continue
        if stripped.isupper() and len(stripped) >= 2:
            result.append(stripped)
    return result


def _build_tavily_retry_query(query: str) -> str | None:
    """Return a simpler retry query when the original Tavily query looks overloaded."""
    base_tokens = [token for token in re.split(r"\s+", query.strip()) if token]
    if not base_tokens:
        return None

    filtered_tokens = [
        token
        for token in base_tokens
        if token.strip(",.;:()[]{}\"'").lower() not in _EXACT_LOOKUP_FILLER
    ]
    identifier_tokens = _identifier_like_tokens(" ".join(filtered_tokens))
    if identifier_tokens:
        candidate = " ".join(identifier_tokens[:3]).strip()
        return candidate if candidate and candidate != query.strip() else None

    candidate = " ".join(filtered_tokens[:8]).strip()
    return candidate if candidate and candidate != query.strip() else None


def _is_empty_search_result(result: ToolResult) -> bool:
    return not result.is_error and result.output.strip() == "No search results found."


def _merge_result_metadata(result: ToolResult, metadata: dict[str, Any]) -> ToolResult:
    merged = dict(result.metadata or {})
    merged.update(metadata)
    result.metadata = merged
    return result


async def handle_web_fetch(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Fetch content from a URL and return it as text or markdown."""
    url = arguments.get("url", "")
    if not url:
        return ToolResult(output="No URL provided.", is_error=True)

    output_format = arguments.get("format", "markdown")
    timeout = arguments.get("timeout", 30)
    user_backend_override = arguments.get("backend")
    backend_name = (
        str(user_backend_override).strip().lower()
        if isinstance(user_backend_override, str)
        else None
    )

    options = _collect_optional_options(
        arguments,
        (
            "query",
            "extract_depth",
            "chunks_per_source",
            "include_images",
            "include_media",
            "media_limit",
        ),
    )

    # Stage C: web_fetch fallback must see the persistent BrowserManager.
    # Merge BROWSER_MANAGER_KEY from shared_runtime_metadata into the per-call
    # dict so all resolver helpers find it without extra traversal.
    runtime_metadata = context.runtime_metadata
    shared = context.shared_runtime_metadata or {}
    if BROWSER_MANAGER_KEY not in runtime_metadata and BROWSER_MANAGER_KEY in shared:
        runtime_metadata[BROWSER_MANAGER_KEY] = shared[BROWSER_MANAGER_KEY]
    controller = get_or_create_controller(runtime_metadata)

    primary_backend = resolve_fetch_backend(runtime_metadata, backend_name)
    primary_label = _backend_label(primary_backend)

    timeout_int = int(timeout) if timeout else 30
    fetch_options = options if options else None

    primary_result = await _run_fetch_with_concurrency(
        controller=controller,
        backend=primary_backend,
        backend_label=primary_label,
        url=url,
        output_format=output_format,
        timeout=timeout_int,
        options=fetch_options,
    )

    if not _should_attempt_browser_fallback(
        result=primary_result,
        primary_backend_name=primary_label,
        runtime_metadata=runtime_metadata,
        user_override=backend_name,
    ):
        if primary_result.is_error:
            return _explain_skipped_fallback(
                primary_label=primary_label,
                primary_result=primary_result,
                runtime_metadata=runtime_metadata,
                user_override=backend_name,
            )
        return build_fetch_tool_result(
            url=url,
            content=primary_result.output,
            metadata=primary_result.metadata,
        )

    browser_backend = get_browser_fetch_backend(runtime_metadata)
    if browser_backend is None:
        return primary_result

    fallback_attempts: list[tuple[str, ToolResult]] = []
    modes_attempted: list[str] = []
    headed_skipped_reason: str | None = None

    logger.info(
        "web: fetch falling back to headless browser backend",
        extra={
            "extra_data": {
                "url_host": host_for(url) or "unknown",
                "primary_backend": primary_label,
                "primary_error": primary_result.output[:120],
            }
        },
    )
    headless_result = await _run_fetch_with_concurrency(
        controller=controller,
        backend=browser_backend,
        backend_label="browser",
        url=url,
        output_format=output_format,
        timeout=timeout_int,
        options=fetch_options,
    )
    modes_attempted.append("headless")
    headless_block_signal = _browser_block_signal(headless_result)
    if not headless_result.is_error and not headless_block_signal:
        return build_fetch_tool_result(
            url=url,
            content=headless_result.output,
            metadata=_annotate_fallback_metadata(
                headless_result,
                fallback_used=True,
                fallback_mode="headless",
                modes_attempted=modes_attempted,
                primary_backend=primary_label,
            ).metadata,
        )
    if headless_block_signal:
        headless_result = ToolResult(
            output=(
                f"Headless browser loaded the page but extraction looked blocked or empty "
                f"({headless_block_signal})."
            ),
            is_error=True,
            metadata=headless_result.metadata,
        )
    fallback_attempts.append(("headless", headless_result))

    headed_backend = (
        get_headed_browser_fetch_backend(runtime_metadata)
        if headed_fallback_enabled(runtime_metadata)
        else None
    )
    if headed_backend is None:
        if runtime_metadata.get("web_browser_fetch_headed_fallback_enabled"):
            headed_skipped_reason = (
                "Headed browser fallback is enabled but the executor browser "
                "manager has not been opted in to headed mode (browser.headed_allowed)."
            )
    else:
        logger.info(
            "web: fetch escalating to headed browser fallback",
            extra={
                "extra_data": {
                    "url_host": host_for(url) or "unknown",
                    "primary_backend": primary_label,
                    "headless_error": headless_result.output[:120],
                }
            },
        )
        headed_result = await _run_fetch_with_concurrency(
            controller=controller,
            backend=headed_backend,
            backend_label="browser",
            url=url,
            output_format=output_format,
            timeout=timeout_int,
            options=fetch_options,
        )
        modes_attempted.append("headed")
        if not headed_result.is_error:
            return build_fetch_tool_result(
                url=url,
                content=headed_result.output,
                metadata=_annotate_fallback_metadata(
                    headed_result,
                    fallback_used=True,
                    fallback_mode="headed",
                    modes_attempted=modes_attempted,
                    primary_backend=primary_label,
                ).metadata,
            )
        fallback_attempts.append(("headed", headed_result))

    return _combined_fallback_failure(
        primary_label=primary_label,
        primary_result=primary_result,
        fallback_results=fallback_attempts,
        modes_attempted=modes_attempted,
        headed_skipped_reason=headed_skipped_reason,
    )


async def _run_fetch_with_concurrency(
    *,
    controller: WebConcurrencyController,
    backend: Any,
    backend_label: str,
    url: str,
    output_format: str,
    timeout: int,
    options: dict[str, Any] | None,
) -> ToolResult:
    """Run a single fetch through the controller's concurrency gates."""
    async with controller.acquire(backend=backend_label, host=host_for(url), op="fetch"):
        result: ToolResult = await backend.fetch(
            url,
            output_format=output_format,
            timeout=timeout,
            options=options,
        )
        return result


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

    query_to_run = str(query).strip()
    query_normalized = False
    retry_attempted = False

    runtime_metadata = context.runtime_metadata
    controller = get_or_create_controller(runtime_metadata)
    backend = resolve_search_backend(runtime_metadata, backend_name)
    backend_label = _backend_label(backend)
    is_tavily_backend = isinstance(backend, TavilyBackend)
    if is_tavily_backend:
        try:
            options = _normalize_tavily_search_options(options)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        query_to_run, options, query_normalized = _normalize_tavily_query(query_to_run, options)

    async with controller.acquire(backend=backend_label, op="search"):
        result = await backend.search(
            query_to_run,
            num_results=num_results,
            options=options if options else None,
        )

    if is_tavily_backend and _is_empty_search_result(result):
        retry_query = _build_tavily_retry_query(query_to_run)
        retry_options = dict(options)
        if retry_query and retry_query != query_to_run:
            retry_attempted = True
            if _identifier_like_tokens(retry_query) and "exact_match" not in retry_options:
                retry_options["exact_match"] = True
            async with controller.acquire(backend=backend_label, op="search"):
                retry_result = await backend.search(
                    retry_query,
                    num_results=num_results,
                    options=retry_options if retry_options else None,
                )
            if not _is_empty_search_result(retry_result):
                return _merge_result_metadata(
                    retry_result,
                    {
                        "tavily_query_normalized": query_normalized,
                        "tavily_retry_attempted": True,
                        "tavily_retry_reason": "empty_results",
                    },
                )

    if is_tavily_backend:
        return _merge_result_metadata(
            result,
            {
                "tavily_query_normalized": query_normalized,
                "tavily_retry_attempted": retry_attempted,
                "tavily_retry_reason": "empty_results" if retry_attempted else None,
            },
        )
    return result


async def handle_web_crawl(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Crawl a website starting from a URL.

    Routes to Tavily's native crawler when ``fetch_backend=tavily`` (it
    delivers significantly better extraction for paying users); otherwise
    falls back to the in-tree DIY BFS crawler that uses whatever fetch
    backend is configured (with auto-browser fallback inherited).
    """
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
            "timeout",
        ),
    )

    runtime_metadata = context.runtime_metadata
    fetch_backend_name = (
        runtime_metadata.get("web_fetch_backend") or runtime_metadata.get("web_backend") or "direct"
    )
    if fetch_backend_name == "tavily":
        tavily = _require_tavily(context)
        if isinstance(tavily, ToolResult):
            return tavily
        return await tavily.crawl(url, options=options if options else None)

    from cognis.tools.executor.web.crawler import crawl_site

    fetch_backend = resolve_fetch_backend(runtime_metadata)
    backend_label = _backend_label(fetch_backend)
    controller = get_or_create_controller(runtime_metadata)
    return await crawl_site(
        url=url,
        fetch_backend=fetch_backend,
        backend_label=backend_label,
        controller=controller,
        options=options if options else None,
    )


async def handle_web_map(arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
    """Map a website's URLs.

    Uses Tavily's native mapper when ``fetch_backend=tavily``; otherwise
    discovers URLs via ``sitemap.xml`` / ``sitemap_index.xml`` and falls
    back to ``<a href>`` enumeration on the start page.
    """
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

    runtime_metadata = context.runtime_metadata
    fetch_backend_name = (
        runtime_metadata.get("web_fetch_backend") or runtime_metadata.get("web_backend") or "direct"
    )
    if fetch_backend_name == "tavily":
        tavily = _require_tavily(context)
        if isinstance(tavily, ToolResult):
            return tavily
        return await tavily.map_site(url, options=options if options else None)

    from cognis.tools.executor.web.sitemap import discover_sitemap_urls

    limit = options.get("limit") if isinstance(options.get("limit"), int) else 200
    same_host_only = not bool(options.get("allow_external"))
    try:
        urls, source = await discover_sitemap_urls(
            url,
            limit=int(limit) if isinstance(limit, int) else 200,
            same_host_only=same_host_only,
        )
    except ValueError as exc:
        return ToolResult(output=str(exc), is_error=True)

    if not urls:
        return ToolResult(
            output=f"No URLs discovered for {url}.",
            metadata={"sitemap_source": source},
        )
    rendered = "\n".join(urls)
    return ToolResult(
        output=f"# URLs discovered ({source})\n\n{rendered}",
        metadata={"sitemap_source": source, "url_count": len(urls)},
    )


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
