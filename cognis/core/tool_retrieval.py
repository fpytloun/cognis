"""Fast per-turn retrieval for relevant tools and skills."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from cognis.logging import get_logger
from cognis.models.tool import ToolDefinition, stable_tool_id, tool_profile_group

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_TOOL_MIN_SCORE = 8.0
_SKILL_MIN_SCORE = 10.0

logger = get_logger(__name__)


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
    log_context: dict[str, Any] | None = None,
) -> list[RetrievedTool]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return []

    query_terms = _tokenize(normalized_query)
    extra_data = _base_log_data(
        target="tools",
        normalized_query=normalized_query,
        query_terms=query_terms,
        limit=limit,
        min_score=min_score,
        log_context=log_context,
    )
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
    scored_candidates: list[dict[str, Any]] = []
    for (tool, display_name, profile_group, _haystack), score in zip(
        candidates, scores, strict=False
    ):
        score += _keyword_boost(
            normalized_query, query_terms, display_name.lower(), tool.description.lower()
        )
        if normalized_query in profile_group.lower():
            score += 6.0
        scored_candidates.append(
            {
                "tool_id": stable_tool_id(tool),
                "name": display_name,
                "profile_group": profile_group,
                "score": round(score, 3),
                "accepted": score >= min_score,
            }
        )
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
    final_results = results[: max(1, limit)]
    _log_retrieval_result(
        message="tool retrieval completed",
        extra_data={
            **extra_data,
            "candidate_count": len(candidates),
            "already_visible_count": len(already_visible_tool_ids),
            "accepted_count": len(final_results),
            "accepted_tools": [
                {"tool_id": item.tool_id, "name": item.name, "score": round(item.score, 3)}
                for item in final_results
            ],
        },
        debug_candidates=scored_candidates,
    )
    return final_results


def retrieve_relevant_skills(
    query: str,
    skills: list[dict[str, Any]],
    *,
    loaded_skill_ids: set[str],
    limit: int = 5,
    min_score: float = _SKILL_MIN_SCORE,
    log_context: dict[str, Any] | None = None,
) -> list[RetrievedSkill]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return []

    query_terms = _tokenize(normalized_query)
    extra_data = _base_log_data(
        target="skills",
        normalized_query=normalized_query,
        query_terms=query_terms,
        limit=limit,
        min_score=min_score,
        log_context=log_context,
    )
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
    scored_candidates: list[dict[str, Any]] = []
    for (skill, name, _haystack), score in zip(candidates, scores, strict=False):
        description = str(skill.get("description") or "").strip()
        score += _keyword_boost(normalized_query, query_terms, name.lower(), description.lower())
        if normalized_query in str(skill.get("skill_id") or "").lower():
            score += 50.0
        scored_candidates.append(
            {
                "skill_id": str(skill.get("skill_id")),
                "name": name,
                "score": round(score, 3),
                "accepted": score >= min_score,
            }
        )
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
    final_results = results[: max(1, limit)]
    _log_retrieval_result(
        message="skill retrieval completed",
        extra_data={
            **extra_data,
            "candidate_count": len(candidates),
            "loaded_skill_count": len(loaded_skill_ids),
            "accepted_count": len(final_results),
            "accepted_skills": [
                {"skill_id": item.skill_id, "name": item.name, "score": round(item.score, 3)}
                for item in final_results
            ],
        },
        debug_candidates=scored_candidates,
    )
    return final_results


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


def _base_log_data(
    *,
    target: str,
    normalized_query: str,
    query_terms: list[str],
    limit: int,
    min_score: float,
    log_context: dict[str, Any] | None,
) -> dict[str, Any]:
    query_bytes = normalized_query.encode("utf-8", errors="ignore")
    return {
        "target": target,
        "query_hash": hashlib.sha256(query_bytes).hexdigest()[:12],
        "query_length": len(normalized_query),
        "query_token_count": len(query_terms),
        "limit": limit,
        "min_score": min_score,
        **(dict(log_context) if log_context else {}),
    }


def _log_retrieval_result(
    *,
    message: str,
    extra_data: dict[str, Any],
    debug_candidates: list[dict[str, Any]],
) -> None:
    logger.info(message, extra={"extra_data": extra_data})
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"{message} payload",
            extra={
                "extra_data": {
                    **extra_data,
                    "candidates": sorted(
                        debug_candidates,
                        key=lambda item: (
                            -float(item.get("score", 0.0)),
                            str(item.get("name") or ""),
                            str(item.get("tool_id") or item.get("skill_id") or ""),
                        ),
                    )[:10],
                }
            },
        )


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
