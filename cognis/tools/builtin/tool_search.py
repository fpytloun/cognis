"""Controller-managed tool discovery definitions and helpers."""

from __future__ import annotations

import math
import re
from typing import Any

from cognis.models.tool import ToolDefinition, ToolSource, stable_tool_id, tool_profile_group

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")

SEARCH_TOOLS_TOOL = ToolDefinition(
    name="search_tools",
    description=(
        "Search for additional tools available in this session. "
        "Use when you need a capability not in your current tool set."
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
) -> list[dict[str, Any]]:
    """Search a permission-filtered tool inventory and return ranked matches."""

    normalized_query = query.strip().lower()
    if not normalized_query:
        return []
    normalized_category = category.strip().lower() if isinstance(category, str) else None
    limit = max(1, min(limit, 20))
    query_terms = _tokenize(normalized_query)
    candidates: list[tuple[ToolDefinition, str, str, str]] = []
    for tool in tools:
        if tool.name == SEARCH_TOOLS_TOOL.name:
            continue
        profile_group = tool_profile_group(tool)
        if normalized_category and normalized_category not in {
            tool.category.lower(),
            profile_group.lower(),
        }:
            continue
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
        score += sum(2.0 for term in query_terms if term in haystack)
        if score <= 0:
            continue
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
                },
            )
        )
    matches.sort(key=lambda item: (-item[0], item[1]["name"], item[1]["tool_id"]))
    return [item for _, item in matches[:limit]]


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
