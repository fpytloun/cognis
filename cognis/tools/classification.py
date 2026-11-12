"""Tool classification helpers for step profiles and previews."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from typing import Any

from cognis import json_codec as json
from cognis.logging import get_logger
from cognis.models.tool import (
    AUTO_PROFILE_GROUPS,
    NATIVE_TOOL_SOURCE_TYPES,
    RESERVED_PROFILE_GROUPS,
    ToolCapability,
    ToolDefinition,
    native_tool_classification_error,
    stable_tool_id,
    tool_capabilities,
    tool_profile_group,
)
from cognis.store.queries import (
    get_tool_classification_override_rows,
    get_tool_classification_rows,
    tool_classification_scope,
)
from cognis.tools.decisions import classify_tool_metadata

logger = get_logger(__name__)

_CLASSIFICATION_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = asyncio.Lock()
_CLASSIFIER_ERROR_DETAIL_LIMIT = 500
_DYNAMIC_SOURCE_TYPES = {"local_mcp", "intaris_mcp", "skill"}
_CLASSIFIER_ERROR_REDACTIONS = (
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"key-[A-Za-z0-9_-]+"),
    re.compile(r"https?://[^\s:@]+:[^\s@]+@"),
    re.compile(r"(?i)(api[_ -]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(['\"]?api[_ -]?key['\"]?\s*:\s*['\"])([^'\"]+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)(access[_ -]?token\s*[=:]\s*)([^\s,;]+)"),
)
_WHITESPACE_RE = re.compile(r"\s+")

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
_READ_ACTIONS = (
    "get",
    "list",
    "search",
    "fetch",
    "read",
    "find",
    "view",
    "describe",
    "query",
    "lookup",
    "show",
    "retrieve",
)
_READ_RESOURCE_SEGMENTS = (
    "alerts",
    "silences",
    "labels",
    "label_values",
    "labelvalues",
    "series",
    "metadata",
)
_WRITE_ACTIONS = (
    "send",
    "post",
    "publish",
    "create",
    "update",
    "modify",
    "set",
    "add",
    "grant",
    "revoke",
    "assign",
    "upload",
    "write",
    "edit",
    "patch",
    "submit",
    "run",
    "execute",
    "trigger",
    "start",
    "stop",
    "enable",
    "disable",
)
_DESTRUCTIVE_ACTIONS = (
    "delete",
    "remove",
    "drop",
    "purge",
    "destroy",
    "cancel",
    "reset",
    "wipe",
    "expire",
)
_READ_SCHEMA_FIELDS = (
    "filter",
    "filters",
    "query",
    "state",
    "status",
    "label",
    "labels",
    "matcher",
    "matchers",
    "selector",
    "start",
    "end",
    "from",
    "to",
    "limit",
    "cursor",
    "page",
    "page_size",
    "pagesize",
)
_WRITE_SCHEMA_FIELDS = (
    "comment",
    "reason",
    "duration",
    "starts_at",
    "startsat",
    "ends_at",
    "endsat",
    "payload",
    "body",
    "data",
    "content",
    "message",
    "text",
    "assignee",
    "quantity",
    "enabled",
    "value",
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

    if tool.source.type in NATIVE_TOOL_SOURCE_TYPES:
        error = native_tool_classification_error(tool)
        if error is not None:
            raise ValueError(f"native tool {tool.name!r}: {error}")
        return tool
    if tool.classification_source and tool.capabilities and tool.profile_group:
        return tool
    profile_group = _heuristic_profile_group(tool)
    capabilities = sorted(_heuristic_capabilities(tool), key=str)
    source = tool.classification_source or "heuristic"
    confidence = tool.classification_confidence
    if confidence is None:
        confidence = 0.6
    return tool.model_copy(
        update={
            "category": tool.category,
            "profile_group": profile_group,
            "capabilities": capabilities,
            "read_only": _classified_read_only(tool, capabilities),
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
    acting_user_email: str | None = None,
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

    updates, _rejected = await _classify_with_llm(
        unresolved,
        llm=llm,
        acting_user_email=acting_user_email,
    )
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
            overlaid.append(
                tool.model_copy(
                    update={
                        "classification_status": (
                            "failed" if str(getattr(row, "status", "")) == "failed" else "pending"
                        )
                    }
                )
            )
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
        try:
            await queue.enqueue_tools(
                [tool for tool in dynamic_tools if stable_tool_id(tool) not in overridden_ids],
                owner_email=owner_email,
            )
        except Exception:
            logger.warning(
                "Failed to enqueue optional tool classifications",
                exc_info=True,
                extra={"extra_data": {"scope_key": scope_key}},
            )
    return apply_persisted_classifications(classified, rows, override_rows)


async def llm_classification_outcomes(
    tools: list[ToolDefinition],
    *,
    llm: Any,
    acting_user_email: str | None = None,
    retry_context: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Return accepted tool classifications and per-tool rejection reasons."""

    return await _classify_with_llm(
        tools,
        llm=llm,
        acting_user_email=acting_user_email,
        retry_context=retry_context,
    )


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
    if _has_destructive_signal(tool, haystack):
        return {ToolCapability.DESTRUCTIVE}
    if tool.read_only:
        return {ToolCapability.READ}
    if _has_write_signal(tool, haystack):
        return {ToolCapability.WRITE}
    caps: set[ToolCapability] = {ToolCapability.WRITE}
    if _has_read_signal(tool, haystack):
        caps = {ToolCapability.READ}
    if tool_profile_group(tool) in {"shell", "browser", "development"} or tool.non_bypassable:
        caps.add(ToolCapability.PRIVILEGED)
    return caps


def _has_destructive_signal(tool: ToolDefinition, haystack: str) -> bool:
    segments = _tool_name_segments(tool)
    return (
        any(haystack.startswith(prefix) for prefix in _DESTRUCTIVE_PREFIXES)
        or any(_contains_token(haystack, action) for action in _DESTRUCTIVE_ACTIONS)
        or any(segment in _DESTRUCTIVE_ACTIONS for segment in segments)
        or _description_leading_action(tool) in _DESTRUCTIVE_ACTIONS
    )


def _has_write_signal(tool: ToolDefinition, haystack: str) -> bool:
    segments = _tool_name_segments(tool)
    if (
        any(haystack.startswith(prefix) for prefix in _WRITE_PREFIXES)
        or any(segment in _WRITE_ACTIONS for segment in segments)
        or _description_leading_action(tool) in _WRITE_ACTIONS
    ):
        return True
    return _schema_contains_any_field(tool.parameters, _WRITE_SCHEMA_FIELDS)


def _has_read_signal(tool: ToolDefinition, haystack: str) -> bool:
    segments = _tool_name_segments(tool)
    if any(haystack.startswith(prefix) for prefix in _READ_PREFIXES):
        return True
    if _description_leading_action(tool) in _READ_ACTIONS:
        return True
    if any(segment in _READ_ACTIONS or segment in _READ_RESOURCE_SEGMENTS for segment in segments):
        return True
    if _schema_contains_any_field(tool.parameters, _READ_SCHEMA_FIELDS):
        return bool(
            _description_leading_action(tool) in _READ_ACTIONS
            or any(segment in _READ_ACTIONS for segment in segments)
        )
    return False


def _description_leading_action(tool: ToolDefinition) -> str | None:
    description = _classification_description(tool.description)
    match = re.match(r"\s*([a-zA-Z][a-zA-Z_-]*)", description)
    if match is None:
        return None
    return _normalize_action(match.group(1))


def _tool_name_segments(tool: ToolDefinition) -> tuple[str, ...]:
    values: list[str] = []
    if tool.source.raw_tool_name:
        values.append(tool.source.raw_tool_name)
    values.append(tool.name.split("__", 1)[1] if "__" in tool.name else tool.name)

    segments: list[str] = []
    for value in values:
        segments.extend(_normalized_segments(value))
    return tuple(dict.fromkeys(segments))


def _normalized_segments(value: str) -> list[str]:
    return [_normalize_action(part) for part in re.split(r"[^a-zA-Z0-9]+", value) if part]


def _normalize_action(value: str) -> str:
    normalized = _normalize_for_matching(value).replace(" ", "_")
    return (
        normalized[:-1]
        if normalized.endswith("s") and normalized[:-1] in _READ_ACTIONS
        else normalized
    )


def _schema_contains_any_field(schema: Any, field_names: tuple[str, ...]) -> bool:
    wanted = {name.lower() for name in field_names}
    return any(field_name in wanted for field_name in _schema_field_names(schema))


def _schema_field_names(schema: Any) -> set[str]:
    field_names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, subschema in properties.items():
                field_names.add(_normalize_action(str(name)))
                field_names.update(_schema_field_names(subschema))
        for key in ("items", "additionalProperties", "anyOf", "oneOf", "allOf"):
            value = schema.get(key)
            if isinstance(value, list):
                for item in value:
                    field_names.update(_schema_field_names(item))
            else:
                field_names.update(_schema_field_names(value))
    elif isinstance(schema, list):
        for item in schema:
            field_names.update(_schema_field_names(item))
    return field_names


async def _classify_with_llm(
    tools: list[ToolDefinition],
    *,
    llm: Any,
    acting_user_email: str | None = None,
    retry_context: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    uncached: list[tuple[str, ToolDefinition, str]] = []
    cached_results: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    retry_context = retry_context or {}
    async with _CACHE_LOCK:
        for tool in tools:
            tool_id = stable_id(tool)
            fingerprint = _tool_fingerprint(tool)
            cache_key = f"{acting_user_email or '__global__'}:{tool_id}:{fingerprint}"
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

    tool_states = [
        {
            "tool_id": tool_id,
            "name": tool.name,
            "raw_tool_name": tool.source.raw_tool_name,
            "description": tool.description,
            "source_type": tool.source.type,
            "current_category": tool.category,
            "read_only": tool.read_only,
            **(
                {
                    "previous_rejection": retry_context[tool_id],
                    "retry_instruction": (
                        "Correct the prior invalid classification. "
                        "return your best valid profile_group and capabilities."
                    ),
                }
                if tool_id in retry_context
                else {}
            ),
        }
        for tool_id, tool, _cache_key in uncached
    ]
    tool_ids = [tool_id for tool_id, _tool, _cache_key in uncached]
    try:
        payload = await classify_tool_metadata(
            llm,
            tool_states,
            acting_user_email=acting_user_email,
        )
    except Exception as exc:
        error = _classifier_error("llm_classify_failed", exc)
        logger.warning(
            "Tool typed classification failed",
            extra={
                "extra_data": {
                    "tool_ids": tool_ids,
                    "error_kind": "classification",
                    "error": error,
                    "acting_user_email_present": acting_user_email is not None,
                }
            },
        )
        return cached_results, {
            **rejected,
            **{tool_id: error for tool_id, _tool, _cache_key in uncached},
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
        raw_tool_id = item.get("tool_id")
        if not isinstance(raw_tool_id, str):
            continue
        tool_id = raw_tool_id
        seen_ids.add(tool_id)
        profile_group = (
            str(item.get("profile_group") or item.get("category") or "development").strip()
            or "development"
        )
        capabilities = _normalize_capabilities(item.get("capabilities"))
        normalized: dict[str, Any] = {
            "profile_group": profile_group,
            "capabilities": capabilities,
            "confidence": float(item.get("confidence") or 0.75),
        }
        error = _validate_profile_group(
            next((tool for candidate_id, tool, _key in uncached if candidate_id == tool_id), None),
            profile_group,
            capabilities,
        )
        if error is not None:
            logger.warning(
                "Rejected LLM tool classification",
                extra={"extra_data": {"tool_id": tool_id, "reason": error}},
            )
            rejected[tool_id] = error
            continue
        results_by_id[tool_id] = normalized
        matched_cache_key = next(
            (key for candidate_id, _tool, key in uncached if candidate_id == tool_id), None
        )
        if matched_cache_key is not None:
            to_cache[matched_cache_key] = normalized
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


def _classifier_error(prefix: str, exc: Exception) -> str:
    detail = _sanitize_classifier_error_detail(str(exc) or exc.__class__.__name__)
    return f"{prefix}:{exc.__class__.__name__}:{detail}"


def _sanitize_classifier_error_detail(detail: str) -> str:
    sanitized = detail
    for pattern in _CLASSIFIER_ERROR_REDACTIONS:
        sanitized = pattern.sub(
            r"\1[redacted]" if pattern.groups else "[redacted-api-key]",
            sanitized,
        )
    sanitized = _WHITESPACE_RE.sub(" ", sanitized).strip()
    if not sanitized:
        sanitized = "no error detail"
    if len(sanitized) > _CLASSIFIER_ERROR_DETAIL_LIMIT:
        sanitized = sanitized[: _CLASSIFIER_ERROR_DETAIL_LIMIT - 3].rstrip() + "..."
    return sanitized


def _validate_profile_group(
    tool: ToolDefinition | None,
    profile_group: str,
    capabilities: list[ToolCapability],
) -> str | None:
    """Validate the contract consistently; keyword group hints are advisory."""
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
