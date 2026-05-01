"""Tool classification helpers for step profiles and previews."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
)
from cognis.logging import get_logger
from cognis.models.tool import (
    AUTO_PROFILE_GROUPS,
    RESERVED_PROFILE_GROUPS,
    ToolCapability,
    ToolDefinition,
    stable_tool_id,
    tool_capabilities,
    tool_profile_group,
)
from cognis.store.queries import (
    get_tool_classification_override_rows,
    get_tool_classification_rows,
    tool_classification_scope,
)

logger = get_logger(__name__)

_CLASSIFICATION_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = asyncio.Lock()
_DYNAMIC_SOURCE_TYPES = {"local_mcp", "intaris_mcp", "skill"}

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

_BROWSER_HINTS = (
    "screenshot",
    "dom",
    "navigate",
    "playwright",
    "devtools",
    "webdriver",
    "selector",
    "browser automation",
)
_COMMUNICATION_HINTS = (
    "slack",
    "gmail",
    "message",
    "channel",
    "thread",
    "chat",
    "discord",
    "telegram",
    "signal",
    "whatsapp",
    "twitter",
    "facebook",
    "tweet",
    "inbox",
)
_OFFICE_HINTS = (
    "calendar",
    "meeting",
    "event",
    "sheet",
    "spreadsheet",
    "doc",
    "document",
    "drive",
    "slides",
    "presentation",
    "form",
    "todo",
    "task",
)
_PERSONAL_HINTS = (
    "order",
    "shopping",
    "delivery",
    "oura",
    "sleep",
    "health",
    "fitness",
    "workout",
    "homeassistant",
    "home assistant",
    "vacuum",
    "light",
    "climate",
    "garage",
    "lock",
)
_DEVELOPMENT_HINTS = (
    "github",
    "gitlab",
    "supabase",
    "next.js",
    "nextjs",
    "xcode",
    "build",
    "test",
    "repo",
    "repository",
    "context7",
    "sequentialthinking",
    "sequential thinking",
    "library",
    "framework",
)
_WEB_HINTS = (
    "http",
    "url",
    "crawl",
    "extract",
    "web",
    "search",
    "fetch",
    "api",
)


def classify_tool_definition(tool: ToolDefinition) -> ToolDefinition:
    """Apply deterministic classification to a tool definition."""

    if tool.classification_source and tool.capabilities and tool.profile_group:
        return tool
    profile_group = _heuristic_profile_group(tool)
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
            "category": tool.category,
            "profile_group": profile_group,
            "capabilities": capabilities,
            "classification_status": tool.classification_status or "ready",
            "classification_source": source,
            "classification_confidence": confidence,
        }
    )


def _read_only_from_capabilities(capabilities: list[ToolCapability]) -> bool:
    capability_set = set(capabilities)
    return ToolCapability.READ in capability_set and not (
        capability_set
        & {ToolCapability.WRITE, ToolCapability.DESTRUCTIVE, ToolCapability.PRIVILEGED}
    )


def _classified_read_only(tool: ToolDefinition, capabilities: list[ToolCapability]) -> bool:
    if tool.source.type not in _DYNAMIC_SOURCE_TYPES:
        return tool.read_only
    return _read_only_from_capabilities(capabilities)


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
            tool_profile_group(tool) not in AUTO_PROFILE_GROUPS
            or not tool.capabilities
            or tool.classification_source == "heuristic"
        )
    ]
    if not unresolved or llm is None:
        return classified

    updates, _rejected = await _classify_with_llm(unresolved, llm=llm)
    by_id = {stable_id(tool): tool for tool in classified}
    for tool_id, payload in updates.items():
        current = by_id.get(tool_id)
        if current is None:
            continue
        profile_group = str(
            payload.get("profile_group") or tool_profile_group(current)
        ).strip() or tool_profile_group(current)
        capabilities = _normalize_capabilities(payload.get("capabilities")) or current.capabilities
        if not capabilities:
            capabilities = sorted(tool_capabilities(current), key=str)
        error = _validate_profile_group(current, profile_group, capabilities)
        if error is not None:
            logger.warning(
                "Rejected LLM tool classification",
                extra={
                    "extra_data": {
                        "tool_id": tool_id,
                        "proposed_profile_group": profile_group,
                        "reason": error,
                    }
                },
            )
            continue
        by_id[tool_id] = current.model_copy(
            update={
                "profile_group": profile_group,
                "capabilities": capabilities,
                "read_only": _classified_read_only(current, capabilities),
                "classification_status": "ready",
                "classification_source": "llm",
                "classification_confidence": float(payload.get("confidence") or 0.75),
            }
        )
    return [by_id[stable_id(tool)] for tool in classified]


def requires_background_classification(tool: ToolDefinition) -> bool:
    """Return whether a tool benefits from background LLM refinement."""

    return tool.source.type in _DYNAMIC_SOURCE_TYPES


def apply_persisted_classifications(
    tools: list[ToolDefinition], rows: list[Any], override_rows: list[Any] | None = None
) -> list[ToolDefinition]:
    """Overlay persisted classification state onto a tool list."""

    row_by_id = {
        str(getattr(row, "tool_id", "")): row
        for row in rows
        if getattr(row, "tool_id", None) is not None
    }
    override_by_id = {
        str(getattr(row, "tool_id", "")): row
        for row in (override_rows or [])
        if getattr(row, "tool_id", None) is not None
    }
    overlaid: list[ToolDefinition] = []
    for tool in classify_tool_definitions_sync(tools):
        override = override_by_id.get(stable_tool_id(tool))
        if override is not None:
            capabilities = _normalize_capabilities(getattr(override, "capabilities", None)) or [
                ToolCapability.READ
            ]
            overlaid.append(
                tool.model_copy(
                    update={
                        "profile_group": str(
                            getattr(override, "profile_group", None) or tool_profile_group(tool)
                        ),
                        "capabilities": capabilities,
                        "read_only": _classified_read_only(tool, capabilities),
                        "classification_status": "ready",
                        "classification_source": "override",
                        "classification_confidence": 1.0,
                    }
                )
            )
            continue
        row = row_by_id.get(stable_tool_id(tool))
        if row is None:
            if requires_background_classification(tool):
                overlaid.append(tool.model_copy(update={"classification_status": "pending"}))
            else:
                overlaid.append(tool)
            continue
        if str(getattr(row, "fingerprint", "")) != tool_fingerprint(tool):
            overlaid.append(tool.model_copy(update={"classification_status": "pending"}))
            continue
        stored_profile_group = str(getattr(row, "category", None) or "").strip()
        capabilities = (
            _normalize_capabilities(getattr(row, "capabilities", None)) or tool.capabilities
        )
        if not capabilities:
            capabilities = sorted(tool_capabilities(tool), key=str)
        if (
            str(getattr(row, "status", "")) != "ready"
            or _validate_profile_group(tool, stored_profile_group, capabilities) is not None
        ):
            overlaid.append(tool.model_copy(update={"classification_status": "pending"}))
            continue
        overlaid.append(
            tool.model_copy(
                update={
                    "profile_group": stored_profile_group,
                    "capabilities": capabilities,
                    "read_only": _classified_read_only(tool, capabilities),
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
        override_rows = await get_tool_classification_override_rows(
            session,
            scope_key=scope_key,
            tool_ids=[stable_tool_id(tool) for tool in dynamic_tools],
        )
    if queue is not None:
        overridden_ids = {str(getattr(row, "tool_id", "")) for row in override_rows}
        await queue.enqueue_tools(
            [tool for tool in dynamic_tools if stable_tool_id(tool) not in overridden_ids],
            owner_email=owner_email,
        )
    return apply_persisted_classifications(classified, rows, override_rows)


async def llm_classification_outcomes(
    tools: list[ToolDefinition], *, llm: Any
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Return accepted tool classifications and per-tool rejection reasons."""

    return await _classify_with_llm(tools, llm=llm)


def _heuristic_profile_group(tool: ToolDefinition) -> str:
    if tool.source.type in {"builtin", "executor"}:
        return tool_profile_group(tool)
    haystack = _tool_haystack(tool)
    if _contains_any(haystack, _BROWSER_HINTS):
        return "browser"
    if _contains_any(haystack, _COMMUNICATION_HINTS):
        return "communication"
    if _contains_any(haystack, _OFFICE_HINTS):
        return "office"
    if _contains_any(haystack, _PERSONAL_HINTS):
        return "personal"
    if any(token in haystack for token in ("file", "filesystem", "path", "repo", "glob", "grep")):
        return "filesystem"
    if any(token in haystack for token in ("bash", "shell", "command", "terminal", "process")):
        return "shell"
    if _contains_any(haystack, _DEVELOPMENT_HINTS):
        return "development"
    if _contains_any(haystack, _WEB_HINTS):
        return "web"
    return "development"


def _heuristic_capabilities(tool: ToolDefinition) -> set[ToolCapability]:
    if tool.capabilities:
        return {ToolCapability(capability) for capability in tool.capabilities}
    haystack = _tool_haystack(tool)
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
    if tool_profile_group(tool) in {"shell", "browser", "development"} or tool.non_bypassable:
        caps.add(ToolCapability.PRIVILEGED)
    return caps


async def _classify_with_llm(
    tools: list[ToolDefinition], *, llm: Any
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    uncached: list[tuple[str, ToolDefinition, str]] = []
    cached_results: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    async with _CACHE_LOCK:
        for tool in tools:
            tool_id = stable_id(tool)
            fingerprint = _tool_fingerprint(tool)
            cache_key = f"{tool_id}:{fingerprint}"
            cached = _CLASSIFICATION_CACHE.get(cache_key)
            if cached is not None:
                error = _validate_profile_group(
                    tool,
                    str(cached.get("profile_group") or cached.get("category") or ""),
                    _normalize_capabilities(cached.get("capabilities")),
                )
                if error is None:
                    cached_results[tool_id] = cached
                    continue
            uncached.append((tool_id, tool, cache_key))
    if not uncached:
        return cached_results, rejected

    messages = [
        {
            "role": "system",
            "content": (
                "Classify tools for an agent runtime. Return JSON object with a top-level 'tools' array. "
                "Each item must include 'tool_id', 'profile_group', 'capabilities', and 'confidence'. "
                "Capabilities must be a subset of ['read','write','privileged','destructive']. "
                "Choose profile_group only from: filesystem, shell, web, browser, development, office, personal, communication. "
                "Never return memory, system, mcp, skill, or general."
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
            response_format={"type": "json_object"},
        )
        payload = extract_json_object(extract_text_from_response(response), label="tool_classifier")
    except Exception:
        logger.warning("Tool LLM classification failed", exc_info=True)
        return cached_results, {
            **rejected,
            **{tool_id: "llm_generate_failed" for tool_id, _tool, _cache_key in uncached},
        }

    results_by_id: dict[str, dict[str, Any]] = dict(cached_results)
    tool_payloads = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tool_payloads, list):
        return results_by_id, {
            **rejected,
            **{tool_id: "invalid_classifier_payload" for tool_id, _tool, _cache_key in uncached},
        }

    to_cache: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for item in tool_payloads:
        if not isinstance(item, dict):
            continue
        tool_id = item.get("tool_id")
        if not isinstance(tool_id, str):
            continue
        seen_ids.add(tool_id)
        normalized = {
            "profile_group": str(
                item.get("profile_group") or item.get("category") or "development"
            ).strip()
            or "development",
            "capabilities": _normalize_capabilities(item.get("capabilities")),
            "confidence": float(item.get("confidence") or 0.75),
        }
        error = _validate_profile_group(
            next((tool for candidate_id, tool, _key in uncached if candidate_id == tool_id), None),
            str(normalized["profile_group"]),
            normalized["capabilities"],
        )
        if error is not None:
            logger.warning(
                "Rejected LLM tool classification",
                extra={"extra_data": {"tool_id": tool_id, "reason": error}},
            )
            rejected[tool_id] = error
            continue
        results_by_id[tool_id] = normalized
        cache_key = next(
            (key for candidate_id, _tool, key in uncached if candidate_id == tool_id), None
        )
        if cache_key is not None:
            to_cache[cache_key] = normalized
    if to_cache:
        async with _CACHE_LOCK:
            _CLASSIFICATION_CACHE.update(to_cache)
    for tool_id, _tool, _cache_key in uncached:
        if tool_id not in results_by_id and tool_id not in rejected:
            rejected[tool_id] = "no_classification_result"
    return results_by_id, rejected


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


def _validate_profile_group(
    tool: ToolDefinition | None,
    profile_group: str,
    capabilities: list[ToolCapability],
) -> str | None:
    normalized_group = profile_group.strip().lower()
    if normalized_group not in AUTO_PROFILE_GROUPS:
        if normalized_group in RESERVED_PROFILE_GROUPS:
            return "reserved_profile_group"
        if normalized_group in {
            "mcp",
            "skill",
            "general",
            "workflow",
            "orchestration",
            "lsp",
            "datetime",
            "context",
            "artifact",
            "schedule",
            "deliverable",
        }:
            return "non_profile_group_category"
        return "unknown_profile_group"
    if not capabilities:
        return "missing_capabilities"
    if tool is None:
        return None
    haystack = _tool_haystack(tool)
    is_browser = _contains_any(haystack, _BROWSER_HINTS)
    is_communication = _contains_any(haystack, _COMMUNICATION_HINTS)
    is_office = _contains_any(haystack, _OFFICE_HINTS)
    is_personal = _contains_any(haystack, _PERSONAL_HINTS)
    is_web = _contains_any(haystack, _WEB_HINTS)
    if is_browser and normalized_group != "browser":
        return "browser_tool_misclassified"
    if normalized_group == "browser" and not is_browser:
        return "browser_group_without_browser_signal"
    if normalized_group == "web" and is_browser:
        return "web_group_for_browser_tool"
    if is_communication and not is_office and normalized_group != "communication":
        return "communication_tool_misclassified"
    if is_office and normalized_group not in {"office", "communication"}:
        return "office_tool_misclassified"
    if is_personal and normalized_group != "personal":
        return "personal_tool_misclassified"
    if normalized_group == "web" and not (is_web or tool.read_only):
        return "web_group_without_web_signal"
    return None


def _tool_haystack(tool: ToolDefinition) -> str:
    return _normalize_for_matching(
        " ".join(
            part
            for part in (
                tool.name,
                tool.source.raw_tool_name or "",
                _classification_description(tool.description),
            )
            if part
        )
    )


def _classification_description(description: str) -> str:
    sections = ("Args:", "Returns:", "Examples:", "Example:")
    summary = description
    for marker in sections:
        summary = summary.split(marker, 1)[0]
    return summary.strip()


def _normalize_for_matching(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _contains_any(haystack: str, tokens: tuple[str, ...]) -> bool:
    return any(_contains_token(haystack, token) for token in tokens)


def _contains_token(haystack: str, token: str) -> bool:
    normalized_token = _normalize_for_matching(token)
    if not normalized_token:
        return False
    return re.search(rf"(?:^| ){re.escape(normalized_token)}(?: |$)", haystack) is not None


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
