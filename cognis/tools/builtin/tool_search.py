"""Controller-managed tool discovery definitions and helpers."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Sequence
from typing import Any

from cognis.logging import get_logger
from cognis.models.tool import (
    NativeToolDefinition as ToolDefinition,
)
from cognis.models.tool import (
    ToolDefinition as BaseToolDefinition,
)
from cognis.models.tool import (
    ToolSource,
    stable_tool_id,
    tool_capabilities,
    tool_profile_group,
)
from cognis.tools.introspection import tool_operation_index

_CLAUDE_MCP_PREFIX = "mcp__cognis__"
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")

logger = get_logger(__name__)

SEARCH_TOOLS_TOOL = ToolDefinition(
    name="search_tools",
    description=(
        "Required discovery path for additional tools available in this session. "
        "You MUST call this tool when you need a capability that is not currently visible, "
        "including filesystem, shell, development, web, skill, or external-service capabilities. "
        "For example, if terminal execution is not visible, search for a shell command or test "
        "runner; use only the exact tools returned by discovery and never guess a hidden name. "
        "Discovery does not add direct-call schemas. Invoke a discovered tool through "
        "call_tool unless its schema is already in the visible tool list. "
        "The optional category is only a hint; omit it unless you are confident. "
        "Examples: shell for terminal or bash execution, filesystem for file tools, "
        "mcp for authorized external service tools."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query for the tool capability you need.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category or profile-group filter such as mcp, "
                    "filesystem, skill, context, or system."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of matches to return.",
            },
        },
        "required": ["query"],
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)

DESCRIBE_TOOL_TOOL = ToolDefinition(
    name="describe_tool",
    description=(
        "Describe one currently authorized tool from the live session inventory. Use this before "
        "calling an unfamiliar tool or a complex mutation to inspect authoritative operations, "
        "mutation kind, omitted/null/array/concurrency semantics, dynamic options, examples, side "
        "effects, and schema version/hash. Accepts callable names and stable tool IDs returned by "
        "search_tools. For a multi-operation tool, pass the exact operation name from search_tools "
        "to avoid loading the full schema union. Omit operation for single-operation tools "
        "(including browser tools); do not infer an operation from a tool name. "
        "It never reveals tools outside the caller's "
        "effective authorization scope."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "minLength": 1,
                "description": "Callable name or stable tool ID from the current session inventory.",
            },
            "operation": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional exact case-sensitive operation name. Use this for multi-operation "
                    "tools to return only that operation's authoritative schema."
                ),
            },
        },
        "required": ["tool"],
        "additionalProperties": False,
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)

VALIDATE_TOOL_CALL_TOOL = ToolDefinition(
    name="validate_tool_call",
    description=(
        "Validate proposed arguments for one currently authorized tool without executing it. Use "
        "this before unfamiliar or high-impact mutations when omitted/null/array semantics or "
        "operation-specific fields are uncertain. Validation uses the exact authoritative "
        "descriptor that generates the model-facing schema and never bypasses authorization."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "minLength": 1,
                "description": "Callable name or stable tool ID from the current session inventory.",
            },
            "arguments": {
                "type": "object",
                "description": "Complete proposed argument object for the target tool.",
            },
        },
        "required": ["tool", "arguments"],
        "additionalProperties": False,
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=True,
)

CALL_TOOL_TOOL = ToolDefinition(
    name="call_tool",
    description=(
        "Call one currently authorized tool from the live session inventory without exposing its "
        "schema. Always supply the envelope: 'tool' is the exact tool_id returned by search_tools, "
        "and 'arguments' is a JSON object containing all target parameters. Never place target "
        "parameters at the top level. Use describe_tool first if the target schema is unknown. "
        "The target receives "
        "the same validation, policy, approval, and execution behavior as a direct tool call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tool": {
                "type": "string",
                "minLength": 1,
                "description": "Stable tool ID or current callable name.",
            },
            "arguments": {
                "type": "object",
                "description": "Complete arguments for the target tool.",
            },
        },
        "required": ["tool", "arguments"],
        "additionalProperties": False,
    },
    source=ToolSource(type="builtin"),
    category="system",
    read_only=False,
)


def resolve_inventory_tool[T: BaseToolDefinition](
    tools: Sequence[T],
    identifier: str,
) -> T | None:
    """Resolve a reference within the supplied inventory, rejecting ambiguous aliases."""

    matches = _inventory_reference_matches(tools, identifier)
    return matches[0] if len(matches) == 1 else None


def _inventory_reference_matches[T: BaseToolDefinition](
    tools: Sequence[T], identifier: str
) -> list[T]:
    normalized = identifier.strip()
    if not normalized:
        return []
    stable_matches = [tool for tool in tools if stable_tool_id(tool) == normalized]
    if stable_matches:
        return stable_matches
    name_matches = [tool for tool in tools if tool.name == normalized]
    if name_matches:
        return name_matches
    alias = _normalize_select_identifier(normalized)
    return [tool for tool in tools if alias in _select_aliases(tool, tool.name)]


def search_inventory(
    tools: list[ToolDefinition],
    query: str,
    *,
    category: str | None = None,
    limit: int = 10,
    already_visible_tool_ids: set[str] | None = None,
    log_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search a permission-filtered tool inventory and return ranked matches."""

    normalized_query = query.strip().lower()
    if not normalized_query:
        return []
    normalized_category = category.strip().lower() if isinstance(category, str) else None
    limit = max(1, min(limit, 20))
    query_terms = tokenize_search_text(normalized_query)
    already_visible_tool_ids = already_visible_tool_ids or set()
    extra_data = {
        "target": "search_tools",
        "query_hash": hashlib.sha256(normalized_query.encode("utf-8", errors="ignore")).hexdigest()[
            :12
        ],
        "query_length": len(normalized_query),
        "query_token_count": len(query_terms),
        "category": normalized_category,
        "limit": limit,
        **(dict(log_context) if log_context else {}),
    }
    if selections := _parse_select_query(normalized_query):
        return _select_inventory(
            tools,
            selections,
            already_visible_tool_ids=already_visible_tool_ids,
            log_context=extra_data,
        )[:limit]
    # Explicit references are lookups, not relevance queries. Include already-visible
    # matches so rediscovery cannot incorrectly imply a capability is unavailable.
    exact_matches = _inventory_reference_matches(tools, query)
    scoped_terms = normalized_query.lstrip("+").split()
    if not exact_matches and len(scoped_terms) == 2:
        exact_matches = _inventory_reference_matches(
            tools, f"mcp:{scoped_terms[0]}:{scoped_terms[1]}"
        )
    if len(exact_matches) > 1:
        return []
    if exact_matches:
        return _select_inventory(
            tools,
            [stable_tool_id(exact_matches[0])],
            already_visible_tool_ids=already_visible_tool_ids,
            log_context=extra_data,
        )
    candidates: list[tuple[ToolDefinition, str, str, str]] = []
    for tool in tools:
        if tool.name == SEARCH_TOOLS_TOOL.name:
            continue
        if stable_tool_id(tool) in already_visible_tool_ids:
            continue
        profile_group = tool_profile_group(tool)
        display_name = (
            tool.source.raw_tool_name
            if tool.source.type == "skill" and tool.source.raw_tool_name
            else tool.name
        )
        haystack = f"{display_name} {tool.description} {tool.category} {profile_group}".lower()
        candidates.append((tool, display_name, profile_group, haystack))

    candidate_scores = bm25_scores(
        [candidate[3] for candidate in candidates],
        query_terms,
    )
    matches: list[tuple[float, dict[str, Any]]] = []
    scored_candidates: list[dict[str, Any]] = []
    for (tool, display_name, profile_group, haystack), bm25_score in zip(
        candidates,
        candidate_scores,
        strict=False,
    ):
        score = bm25_score
        if normalized_query in display_name.lower():
            score += 50.0
        if normalized_query in tool.description.lower():
            score += 25.0
        if normalized_query in tool.category.lower():
            score += 10.0
        if normalized_query in profile_group.lower():
            score += 10.0
        if normalized_category:
            category_match = normalized_category == tool.category.lower()
            profile_match = normalized_category == profile_group.lower()
            if category_match:
                score += 2.0
            if profile_match:
                score += 2.0
        score += sum(2.0 for term in query_terms if term in haystack)
        scored_candidates.append(
            {
                "tool_id": stable_tool_id(tool),
                "name": display_name,
                "profile_group": profile_group,
                "score": round(score, 3),
                "accepted": score > 0,
                "category_hint_match": bool(
                    normalized_category
                    and normalized_category in {tool.category.lower(), profile_group.lower()}
                ),
            }
        )
        if score <= 0:
            continue
        handle = _tool_handle(tool, display_name, profile_group, confidence=round(score, 3))
        matches.append(
            (
                score,
                {
                    "tool_id": stable_tool_id(tool),
                    "name": display_name,
                    "description": tool.description,
                    "category": tool.category,
                    "profile_group": profile_group,
                    "source": tool.source.model_dump(mode="json"),
                    "handle": handle,
                    **tool_operation_index(tool),
                },
            )
        )
    matches.sort(key=lambda item: (-item[0], item[1]["name"], item[1]["tool_id"]))
    final_matches = [item for _, item in matches[:limit]]
    logger.info(
        "search_tools retrieval completed",
        extra={
            "extra_data": {
                **extra_data,
                "candidate_count": len(candidates),
                "already_visible_count": len(already_visible_tool_ids),
                "accepted_count": len(final_matches),
                "accepted_tools": [
                    {
                        "tool_id": item["tool_id"],
                        "name": item["name"],
                        "profile_group": item["profile_group"],
                    }
                    for item in final_matches
                ],
            }
        },
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "search_tools retrieval payload",
            extra={
                "extra_data": {
                    **extra_data,
                    "candidate_count": len(candidates),
                    "already_visible_count": len(already_visible_tool_ids),
                    "candidates": sorted(
                        scored_candidates,
                        key=lambda item: (
                            -float(item.get("score", 0.0)),
                            str(item.get("name") or ""),
                            str(item.get("tool_id") or ""),
                        ),
                    )[:10],
                }
            },
        )
    return final_matches


def _parse_select_query(normalized_query: str) -> list[str]:
    if not normalized_query.startswith("select:"):
        return []
    raw_selectors = normalized_query.removeprefix("select:").split(",")
    return [selector.strip() for selector in raw_selectors if selector.strip()]


def _select_inventory(
    tools: list[ToolDefinition],
    selectors: list[str],
    *,
    already_visible_tool_ids: set[str],
    log_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return exact selected tools requested by a native tool-search call."""

    candidates: list[tuple[ToolDefinition, str, str, bool]] = []
    for tool in tools:
        if tool.name == SEARCH_TOOLS_TOOL.name:
            continue
        display_name = (
            tool.source.raw_tool_name
            if tool.source.type == "skill" and tool.source.raw_tool_name
            else tool.name
        )
        candidates.append(
            (
                tool,
                display_name,
                tool_profile_group(tool),
                stable_tool_id(tool) in already_visible_tool_ids,
            )
        )

    matches: list[dict[str, Any]] = []
    matched_selectors: list[str] = []
    already_visible_selectors: list[str] = []
    unmatched_selectors: list[str] = []
    seen_tool_ids: set[str] = set()
    for selector in selectors:
        resolved = resolve_inventory_tool(tools, selector)
        if resolved is None or resolved.name == SEARCH_TOOLS_TOOL.name:
            unmatched_selectors.append(selector)
            continue
        tool, display_name, profile_group, already_visible = next(
            candidate for candidate in candidates if candidate[0] is resolved
        )
        tool_id = stable_tool_id(tool)
        if tool_id in seen_tool_ids:
            continue
        seen_tool_ids.add(tool_id)
        matched_selectors.append(selector)
        if already_visible:
            already_visible_selectors.append(selector)
        matches.append(
            {
                "tool_id": tool_id,
                "name": display_name,
                "description": tool.description,
                "category": tool.category,
                "profile_group": profile_group,
                "already_visible": already_visible,
                "source": tool.source.model_dump(mode="json"),
                "handle": _tool_handle(tool, display_name, profile_group, confidence=100.0),
                **tool_operation_index(tool),
            }
        )

    logger.info(
        "search_tools select completed",
        extra={
            "extra_data": {
                **log_context,
                "selector_count": len(selectors),
                "matched_count": len(matched_selectors),
                "already_visible_match_count": len(already_visible_selectors),
                "unmatched_count": len(unmatched_selectors),
                "matched_tools": [
                    {"tool_id": item["tool_id"], "name": item["name"]} for item in matches
                ],
            }
        },
    )
    if unmatched_selectors:
        logger.debug(
            "search_tools select unmatched selectors",
            extra={
                "extra_data": {
                    **log_context,
                    "unmatched_selectors": unmatched_selectors[:20],
                }
            },
        )
    return matches


def _normalize_select_identifier(selector: str) -> str:
    normalized = selector.strip().lower()
    if normalized.startswith(_CLAUDE_MCP_PREFIX):
        normalized = normalized[len(_CLAUDE_MCP_PREFIX) :]
    return normalized


def _select_aliases(tool: BaseToolDefinition, display_name: str) -> set[str]:
    aliases = {
        tool.name,
        display_name,
        stable_tool_id(tool),
        str(tool.source.raw_tool_name or ""),
    }
    raw_name = tool.source.raw_tool_name
    server_id = tool.source.server_id
    if raw_name and server_id:
        aliases.update(
            {
                f"mcp_{server_id}__{raw_name}",
                f"mcp__{server_id}__{raw_name}",
                f"mcp:{server_id}:{raw_name}",
            }
        )
    if raw_name and tool.source.server_name:
        # Persisted skill links use qualified server names; IDs remain canonical.
        aliases.add(f"mcp:{tool.source.server_name}:{raw_name}")
    normalized_aliases = {_normalize_select_identifier(alias) for alias in aliases if alias}
    expanded_aliases: set[str] = set()
    for alias in normalized_aliases:
        expanded_aliases.add(alias)
        if alias.startswith("mcp__"):
            expanded_aliases.add("mcp_" + alias[len("mcp__") :])
        if alias.startswith("mcp_") and not alias.startswith("mcp__"):
            expanded_aliases.add("mcp__" + alias[len("mcp_") :])
    return expanded_aliases


def _tool_handle(
    tool: ToolDefinition,
    display_name: str,
    profile_group: str,
    *,
    confidence: float,
) -> dict[str, Any]:
    return {
        "tool_id": stable_tool_id(tool),
        "name": display_name,
        "callable_name": tool.name,
        "scope": "session",
        "category": tool.category,
        "profile_group": profile_group,
        "source": tool.source.model_dump(mode="json"),
        "capabilities": sorted(str(capability) for capability in tool_capabilities(tool)),
        "read_only": tool.read_only,
        "confidence": confidence,
        "permission_scope": "current_session_effective_inventory",
    }


def tokenize_search_text(text: str) -> list[str]:
    """Split text into lowercase search terms."""
    return _TOKEN_PATTERN.findall(text.lower())


def bm25_scores(documents: list[str], query_terms: list[str]) -> list[float]:
    """Score documents against query terms with BM25."""
    if not documents or not query_terms:
        return [0.0 for _ in documents]

    tokenized_docs = [tokenize_search_text(document) for document in documents]
    avgdl = sum(len(doc) for doc in tokenized_docs) / max(1, len(tokenized_docs))
    document_frequencies: dict[str, int] = {}
    for doc in tokenized_docs:
        for term in set(doc):
            document_frequencies[term] = document_frequencies.get(term, 0) + 1

    k1 = 1.5
    b = 0.75
    scores: list[float] = []
    total_docs = len(tokenized_docs)
    for doc in tokenized_docs:
        score = 0.0
        doc_length = len(doc)
        for term in query_terms:
            freq = doc.count(term)
            if freq == 0:
                continue
            df = document_frequencies.get(term, 0)
            idf = math.log(1 + ((total_docs - df + 0.5) / (df + 0.5)))
            norm = freq + k1 * (1 - b + b * (doc_length / max(avgdl, 1.0)))
            score += idf * ((freq * (k1 + 1)) / norm)
        scores.append(score)
    return scores
