"""Fast per-turn retrieval for relevant tools and skills."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from cognis.models.tool import ToolDefinition, stable_tool_id, tool_profile_group

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_TOOL_MIN_SCORE = 8.0
_SKILL_MIN_SCORE = 10.0


@dataclass(frozen=True, slots=True)
class RetrievedTool:
    tool_id: str
    name: str
    score: float


@dataclass(frozen=True, slots=True)
class RetrievedSkill:
    skill_id: str
    name: str
    description: str
    score: float


def retrieve_relevant_tools(
    query: str,
    tools: list[ToolDefinition],
    *,
    already_visible_tool_ids: set[str],
    limit: int = 15,
    min_score: float = _TOOL_MIN_SCORE,
) -> list[RetrievedTool]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return []

    query_terms = _tokenize(normalized_query)
    candidates: list[tuple[ToolDefinition, str, str, str]] = []
    for tool in tools:
        tool_id = stable_tool_id(tool)
        if tool_id in already_visible_tool_ids:
            continue
        display_name = tool.source.raw_tool_name if tool.source.raw_tool_name else tool.name
        profile_group = tool_profile_group(tool)
        haystack = f"{display_name} {tool.description} {tool.category} {profile_group}".lower()
        candidates.append((tool, display_name, profile_group, haystack))

    scores = _bm25_scores([candidate[3] for candidate in candidates], query_terms)
    results: list[RetrievedTool] = []
    for (tool, display_name, profile_group, _haystack), score in zip(
        candidates, scores, strict=False
    ):
        score += _keyword_boost(
            normalized_query, query_terms, display_name.lower(), tool.description.lower()
        )
        if normalized_query in profile_group.lower():
            score += 6.0
        if score < min_score:
            continue
        results.append(
            RetrievedTool(
                tool_id=stable_tool_id(tool),
                name=display_name,
                score=score,
            )
        )
    results.sort(key=lambda item: (-item.score, item.name, item.tool_id))
    return results[: max(1, limit)]


def retrieve_relevant_skills(
    query: str,
    skills: list[dict[str, Any]],
    *,
    loaded_skill_ids: set[str],
    limit: int = 5,
    min_score: float = _SKILL_MIN_SCORE,
) -> list[RetrievedSkill]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return []

    query_terms = _tokenize(normalized_query)
    candidates: list[tuple[dict[str, Any], str, str]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        skill_id = str(skill.get("skill_id") or "").strip()
        name = str(skill.get("name") or "").strip()
        if not skill_id or not name or skill_id in loaded_skill_ids:
            continue
        description = str(skill.get("description") or "").strip()
        tags = ", ".join(str(tag) for tag in skill.get("tags") or [] if isinstance(tag, str))
        haystack = f"{skill_id} {name} {description} {tags}".lower()
        candidates.append((skill, name, haystack))

    scores = _bm25_scores([candidate[2] for candidate in candidates], query_terms)
    results: list[RetrievedSkill] = []
    for (skill, name, _haystack), score in zip(candidates, scores, strict=False):
        description = str(skill.get("description") or "").strip()
        score += _keyword_boost(normalized_query, query_terms, name.lower(), description.lower())
        if normalized_query in str(skill.get("skill_id") or "").lower():
            score += 50.0
        if score < min_score:
            continue
        results.append(
            RetrievedSkill(
                skill_id=str(skill.get("skill_id")),
                name=name,
                description=description,
                score=score,
            )
        )
    results.sort(key=lambda item: (-item.score, item.name, item.skill_id))
    return results[: max(1, limit)]


def _keyword_boost(
    normalized_query: str,
    query_terms: list[str],
    name: str,
    description: str,
) -> float:
    score = 0.0
    if normalized_query in name:
        score += 50.0
    if normalized_query in description:
        score += 20.0
    score += sum(3.0 for term in query_terms if term in name)
    score += sum(1.0 for term in query_terms if term in description)
    return score


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
