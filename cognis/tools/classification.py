"""Tool classification helpers for step profiles and previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any

from cognis.core.json_utils import extract_json_object, extract_text_from_response
from cognis.logging import get_logger
from cognis.models.tool import ToolCapability, ToolDefinition, stable_tool_id, tool_capabilities
from cognis.store.queries import get_tool_classification_rows, tool_classification_scope

logger = get_logger(__name__)

_CLASSIFICATION_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = asyncio.Lock()

_READ_PREFIXES = (
    "get_",
    "list_",
    "search_",
    "fetch_",
    "read_",
    "find_",
    "view_",
    "describe_",
    "query_",
    "lookup_",
    "show_",
)
_WRITE_PREFIXES = (
    "send_",
    "post_",
    "publish_",
    "create_",
    "update_",
    "modify_",
    "set_",
    "add_",
    "grant_",
    "revoke_",
    "assign_",
    "upload_",
    "write_",
    "edit_",
    "patch_",
)
_DESTRUCTIVE_PREFIXES = (
    "delete_",
    "remove_",
    "drop_",
    "purge_",
    "destroy_",
    "cancel_all_",
    "reset_",
    "wipe_",
)


def classify_tool_definition(tool: ToolDefinition) -> ToolDefinition:
    """Apply deterministic classification to a tool definition."""

    if tool.classification_source and tool.capabilities:
        return tool
    category = _heuristic_category(tool)
    capabilities = sorted(_heuristic_capabilities(tool), key=str)
    source = tool.classification_source or (
        "declared"
        if tool.source.type in {"builtin", "executor"} and tool.category not in {"skill", "mcp"}
        else "heuristic"
    )
    confidence = tool.classification_confidence
    if confidence is None:
        confidence = 1.0 if source == "declared" else 0.6
    return tool.model_copy(
        update={
            "category": category,
            "capabilities": capabilities,
            "classification_status": tool.classification_status or "ready",
            "classification_source": source,
            "classification_confidence": confidence,
        }
    )


def classify_tool_definitions_sync(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """Classify tools without calling the LLM."""

    return [classify_tool_definition(tool) for tool in tools]


async def classify_tool_definitions(
    tools: list[ToolDefinition],
    *,
    llm: Any | None = None,
) -> list[ToolDefinition]:
    """Classify tools, using the LLM for unresolved dynamic tools when needed."""

    classified = classify_tool_definitions_sync(tools)
    unresolved = [
        tool
        for tool in classified
        if tool.source.type in {"local_mcp", "intaris_mcp", "skill"}
        and (
            tool.category in {"mcp", "skill", "general"}
            or not tool.capabilities
            or tool.classification_source == "heuristic"
        )
    ]
    if not unresolved or llm is None:
        return classified

    updates = await _classify_with_llm(unresolved, llm=llm)
    by_id = {stable_id(tool): tool for tool in classified}
    for tool_id, payload in updates.items():
        current = by_id.get(tool_id)
        if current is None:
            continue
        category = str(payload.get("category") or current.category).strip() or current.category
        capabilities = _normalize_capabilities(payload.get("capabilities")) or current.capabilities
        if not capabilities:
            capabilities = sorted(tool_capabilities(current), key=str)
        by_id[tool_id] = current.model_copy(
            update={
                "category": category,
                "capabilities": capabilities,
                "classification_status": "ready",
                "classification_source": "llm",
                "classification_confidence": float(payload.get("confidence") or 0.75),
            }
        )
    return [by_id[stable_id(tool)] for tool in classified]


def requires_background_classification(tool: ToolDefinition) -> bool:
    """Return whether a tool benefits from background LLM refinement."""

    return tool.source.type in {"local_mcp", "intaris_mcp", "skill"}


def apply_persisted_classifications(
    tools: list[ToolDefinition], rows: list[Any]
) -> list[ToolDefinition]:
    """Overlay persisted classification state onto a tool list."""

    row_by_id = {
        str(getattr(row, "tool_id", "")): row
        for row in rows
        if getattr(row, "tool_id", None) is not None
    }
    overlaid: list[ToolDefinition] = []
    for tool in classify_tool_definitions_sync(tools):
        row = row_by_id.get(stable_tool_id(tool))
        if row is None or getattr(row, "fingerprint", None) != tool_fingerprint(tool):
            if requires_background_classification(tool):
                overlaid.append(tool.model_copy(update={"classification_status": "pending"}))
            else:
                overlaid.append(tool)
            continue
        if str(getattr(row, "status", "")) != "ready":
            overlaid.append(tool.model_copy(update={"classification_status": "pending"}))
            continue
        capabilities = _normalize_capabilities(getattr(row, "capabilities", None)) or tool.capabilities
        if not capabilities:
            capabilities = sorted(tool_capabilities(tool), key=str)
        overlaid.append(
            tool.model_copy(
                update={
                    "category": str(getattr(row, "category", None) or tool.category),
                    "capabilities": capabilities,
                    "classification_status": "ready",
                    "classification_source": getattr(row, "classification_source", None)
                    or tool.classification_source,
                    "classification_confidence": getattr(row, "classification_confidence", None)
                    if getattr(row, "classification_confidence", None) is not None
                    else tool.classification_confidence,
                }
            )
        )
    return overlaid


async def resolve_tool_classifications(
    tools: list[ToolDefinition],
    *,
    session_factory: Callable[[], Any],
    owner_email: str | None,
    queue: Any | None = None,
) -> list[ToolDefinition]:
    """Resolve classifications without blocking on live LLM calls."""

    classified = classify_tool_definitions_sync(tools)
    dynamic_tools = [tool for tool in classified if requires_background_classification(tool)]
    if not dynamic_tools:
        return classified
    scope_key = tool_classification_scope(owner_email)
    async with session_factory() as session:
        rows = await get_tool_classification_rows(
            session,
            scope_key=scope_key,
            tool_ids=[stable_tool_id(tool) for tool in dynamic_tools],
        )
    if queue is not None:
        await queue.enqueue_tools(dynamic_tools, owner_email=owner_email)
    return apply_persisted_classifications(classified, rows)


def _heuristic_category(tool: ToolDefinition) -> str:
    if tool.category not in {"skill", "mcp", "general"}:
        return tool.category
    haystack = " ".join(
        part
        for part in (
            tool.name,
            tool.source.raw_tool_name or "",
            tool.description,
        )
        if part
    ).lower()
    if any(token in haystack for token in ("browser", "page", "click", "screenshot", "dom")):
        return "browser"
    if any(token in haystack for token in ("http", "url", "crawl", "extract", "web", "search")):
        return "web"
    if any(token in haystack for token in ("file", "filesystem", "path", "repo", "glob", "grep")):
        return "filesystem"
    if any(token in haystack for token in ("bash", "shell", "command", "terminal", "process")):
        return "shell"
    if any(token in haystack for token in ("memory", "remember", "recall", "knowledge")):
        return "memory"
    if any(token in haystack for token in ("calendar", "time", "date", "timezone")):
        return "datetime"
    if any(token in haystack for token in ("git", "diff", "commit", "pull request", "github")):
        return "orchestration"
    return tool.category if tool.category != "general" else "mcp"


def _heuristic_capabilities(tool: ToolDefinition) -> set[ToolCapability]:
    if tool.capabilities:
        return {ToolCapability(capability) for capability in tool.capabilities}
    haystack = " ".join(
        part
        for part in (
            tool.name,
            tool.source.raw_tool_name or "",
            tool.description,
        )
        if part
    ).lower()
    if any(haystack.startswith(prefix) for prefix in _DESTRUCTIVE_PREFIXES) or any(
        token in haystack for token in ("delete", "destroy", "purge", "drop", "wipe")
    ):
        return {ToolCapability.DESTRUCTIVE}
    if tool.read_only:
        return {ToolCapability.READ}
    caps: set[ToolCapability] = {ToolCapability.WRITE}
    if any(haystack.startswith(prefix) for prefix in _READ_PREFIXES):
        caps = {ToolCapability.READ}
    elif any(haystack.startswith(prefix) for prefix in _WRITE_PREFIXES):
        caps = {ToolCapability.WRITE}
    if tool.category in {"shell", "browser", "orchestration", "skill"} or tool.non_bypassable:
        caps.add(ToolCapability.PRIVILEGED)
    return caps


async def _classify_with_llm(tools: list[ToolDefinition], *, llm: Any) -> dict[str, dict[str, Any]]:
    uncached: list[tuple[str, ToolDefinition, str]] = []
    cached_results: dict[str, dict[str, Any]] = {}
    async with _CACHE_LOCK:
        for tool in tools:
            tool_id = stable_id(tool)
            fingerprint = _tool_fingerprint(tool)
            cache_key = f"{tool_id}:{fingerprint}"
            cached = _CLASSIFICATION_CACHE.get(cache_key)
            if cached is not None:
                cached_results[tool_id] = cached
                continue
            uncached.append((tool_id, tool, cache_key))
    if not uncached:
        return cached_results

    messages = [
        {
            "role": "system",
            "content": (
                "Classify tools for an agent runtime. Return JSON object with a top-level 'tools' array. "
                "Each item must include 'tool_id', 'category', 'capabilities', and 'confidence'. "
                "Capabilities must be a subset of ['read','write','privileged','destructive']. "
                "Prefer existing categories when plausible: filesystem, shell, web, browser, memory, datetime, orchestration, lsp, system, workflow, image, schedule, mcp, skill."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "tools": [
                        {
                            "tool_id": tool_id,
                            "name": tool.name,
                            "raw_tool_name": tool.source.raw_tool_name,
                            "description": tool.description,
                            "source_type": tool.source.type,
                            "current_category": tool.category,
                            "read_only": tool.read_only,
                            "parameters": tool.parameters,
                        }
                        for tool_id, tool, _cache_key in uncached
                    ]
                },
                ensure_ascii=True,
            ),
        },
    ]
    try:
        response = await llm.generate(
            messages,
            task_type="classifier",
            temperature=0,
            max_retries=1,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        payload = extract_json_object(extract_text_from_response(response), label="tool_classifier")
    except Exception:
        logger.warning("Tool LLM classification failed", exc_info=True)
        return cached_results

    results_by_id: dict[str, dict[str, Any]] = dict(cached_results)
    tool_payloads = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tool_payloads, list):
        return results_by_id

    to_cache: dict[str, dict[str, Any]] = {}
    for item in tool_payloads:
        if not isinstance(item, dict):
            continue
        tool_id = item.get("tool_id")
        if not isinstance(tool_id, str):
            continue
        normalized = {
            "category": str(item.get("category") or "mcp").strip() or "mcp",
            "capabilities": _normalize_capabilities(item.get("capabilities")),
            "confidence": float(item.get("confidence") or 0.75),
        }
        results_by_id[tool_id] = normalized
        cache_key = next(
            (key for candidate_id, _tool, key in uncached if candidate_id == tool_id), None
        )
        if cache_key is not None:
            to_cache[cache_key] = normalized
    if to_cache:
        async with _CACHE_LOCK:
            _CLASSIFICATION_CACHE.update(to_cache)
    return results_by_id


def _normalize_capabilities(value: Any) -> list[ToolCapability]:
    if not isinstance(value, list):
        return []
    normalized: list[ToolCapability] = []
    for item in value:
        try:
            capability = ToolCapability(str(item))
        except ValueError:
            continue
        if capability not in normalized:
            normalized.append(capability)
    return normalized


def _tool_fingerprint(tool: ToolDefinition) -> str:
    payload = json.dumps(
        {
            "name": tool.name,
            "source_type": tool.source.type,
            "raw_tool_name": tool.source.raw_tool_name,
            "description": tool.description,
            "parameters": tool.parameters,
            "read_only": tool.read_only,
            "non_bypassable": tool.non_bypassable,
            "timeout_seconds": tool.timeout_seconds,
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()


def tool_fingerprint(tool: ToolDefinition) -> str:
    """Return a stable fingerprint for persisted tool classification rows."""

    return _tool_fingerprint(tool)


def stable_id(tool: ToolDefinition) -> str:
    return stable_tool_id(tool)
