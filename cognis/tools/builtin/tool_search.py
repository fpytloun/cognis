"""Controller-managed tool discovery definitions and helpers."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any

from cognis.logging import get_logger
from cognis.models.tool import (
    ToolDefinition,
    ToolSource,
    stable_tool_id,
    tool_capabilities,
    tool_profile_group,
)

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")

logger = get_logger(__name__)

SEARCH_TOOLS_TOOL = ToolDefinition(
    name="search_tools",
    description=(
        "Search for additional tools available in this session. "
        "Use when you need a capability not in your current tool set. "
        "The optional category is only a hint; omit it unless you are confident. "
        "Examples: shell for terminal/bash tools, filesystem for file tools, "
        "mcp for external service tools."
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
    query_terms = _tokenize(normalized_query)
    already_visible_tool_ids = already_visible_tool_ids or set()
    extra_data = {
        "target": "search_tools",
        "query_hash": hashlib.sha256(
            normalized_query.encode("utf-8", errors="ignore")
        ).hexdigest()[:12],
        "query_length": len(normalized_query),
        "query_token_count": len(query_terms),
        "category": normalized_category,
        "limit": limit,
        **(dict(log_context) if log_context else {}),
    }
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

    bm25_scores = _bm25_scores(
        [candidate[3] for candidate in candidates],
        query_terms,
    )
    matches: list[tuple[float, dict[str, Any]]] = []
    scored_candidates: list[dict[str, Any]] = []
    for (tool, display_name, profile_group, haystack), bm25_score in zip(
        candidates,
        bm25_scores,
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
        handle = {
            "tool_id": stable_tool_id(tool),
            "name": display_name,
            "callable_name": tool.name,
            "scope": "session",
            "category": tool.category,
            "profile_group": profile_group,
            "source": tool.source.model_dump(mode="json"),
            "capabilities": sorted(str(capability) for capability in tool_capabilities(tool)),
            "read_only": tool.read_only,
            "confidence": round(score, 3),
            "permission_scope": "current_session_effective_inventory",
        }
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


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def _bm25_scores(documents: list[str], query_terms: list[str]) -> list[float]:
    if not documents or not query_terms:
        return [0.0 for _ in documents]

    tokenized_docs = [_tokenize(document) for document in documents]
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
