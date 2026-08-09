"""Domain-neutral semantic quality, provenance, and candidate comparison."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

_WORD_RE = re.compile(r"\b[\w'-]+\b")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_NOISE_RE = re.compile(
    r"\b(?:subscribe|sign\s+up|advertisement|cookie|newsletter|related\s+articles|share\s+this)\b",
    re.IGNORECASE,
)
_NAVIGATION_RE = re.compile(
    r"\b(?:home|about|contact|menu|navigation|privacy|terms|search|next|previous)\b", re.IGNORECASE
)
_STATUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("blocked", re.compile(r"\b(?:access denied|verify you are human|captcha|blocked)\b", re.I)),
    ("login_required", re.compile(r"\b(?:log\s*in|sign\s*in|login|create an account)\b", re.I)),
    (
        "paywall",
        re.compile(r"\b(?:paywall|subscribe to read|subscription required|members only)\b", re.I),
    ),
    ("interstitial", re.compile(r"\b(?:please wait|loading|consent|enable javascript)\b", re.I)),
)
SEMANTIC_STATUSES = frozenset(
    {
        "complete",
        "partial",
        "empty",
        "title_only",
        "navigation_only",
        "login_required",
        "paywall",
        "interstitial",
        "blocked",
        "unavailable",
    }
)
STATUS_RANKS = {
    "empty": 0,
    "blocked": 0,
    "unavailable": 0,
    "title_only": 1,
    "interstitial": 1,
    "navigation_only": 2,
    "login_required": 2,
    "paywall": 2,
    "partial": 3,
    "complete": 4,
}


@dataclass(frozen=True, slots=True)
class SemanticQuality:
    score: float
    rank: int
    status: str
    signals: tuple[str, ...]
    characters: int
    words: int
    paragraphs: int
    noise_hits: int
    blocked: bool

    @property
    def label(self) -> str:
        return self.status

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "label": self.status,
            "score": self.score,
            "rank": self.rank,
            "signals": list(self.signals),
            "characters": self.characters,
            "words": self.words,
            "paragraphs": self.paragraphs,
            "noise_hits": self.noise_hits,
            "blocked": self.blocked,
        }


def assess_semantic_quality(content: str, *, title: str | None = None) -> SemanticQuality:
    """Classify extracted content; complete requires substantive body evidence."""
    text = re.sub(r"\s+", " ", content).strip()
    characters = len(text)
    words = len(_WORD_RE.findall(text))
    paragraphs = sum(bool(part.strip()) for part in re.split(r"\n\s*\n", content))
    link_count = len(_MARKDOWN_LINK_RE.findall(content))
    noise_hits = len(_NOISE_RE.findall(text))
    signals: list[str] = []
    status = "empty"
    if text:
        for candidate_status, pattern in _STATUS_PATTERNS:
            marker_is_dominant = characters < 2_000 or words < 120
            if marker_is_dominant and pattern.search(text):
                status = candidate_status
                signals.append(candidate_status)
                break
        if not signals and title and text.casefold() == title.strip().casefold():
            status = "title_only"
            signals.append("title_only")
        elif not signals and (
            (words <= 30 and _NAVIGATION_RE.search(text))
            or (
                link_count >= 12
                and words > 0
                and link_count / max(words, 1) >= 0.08
                and paragraphs <= 8
            )
        ):
            status = "navigation_only"
            signals.append("navigation_density")
        elif not signals:
            substantive = words >= 30 and characters >= 200 and paragraphs >= 1
            status = "complete" if substantive else "partial"
            signals.append("substantive_body" if substantive else "thin_body")
    if noise_hits:
        signals.append("navigation_noise")
    score = min(100.0, characters / 8 + words / 3 + paragraphs * 8)
    if status in {
        "empty",
        "title_only",
        "navigation_only",
        "login_required",
        "paywall",
        "interstitial",
        "blocked",
    }:
        score = min(score, 20.0)
    return SemanticQuality(
        round(max(score, 0.0), 3),
        STATUS_RANKS[status],
        status,
        tuple(signals),
        characters,
        words,
        paragraphs,
        noise_hits,
        status == "blocked",
    )


def compare_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Compare candidates deterministically, preferring status rank then score."""
    comparisons: list[dict[str, Any]] = []
    for candidate in candidates:
        quality = assess_semantic_quality(
            str(candidate.get("content") or ""), title=candidate.get("title")
        )
        candidate["_semantic_quality"] = quality
        candidate["_comparison_score"] = quality.score + float(candidate.get("score") or 0)
        comparisons.append(
            {
                "source": str(candidate.get("source") or "unknown"),
                "status": quality.status,
                "quality": quality.status,
                "score": quality.score,
                "rank": quality.rank,
                "signals": list(quality.signals),
            }
        )
    if not candidates:
        return None, comparisons
    winner = max(
        enumerate(candidates),
        key=lambda pair: (
            pair[1]["_semantic_quality"].rank,
            pair[1]["_comparison_score"],
            -pair[0],
        ),
    )[1]
    comparisons.sort(key=lambda row: (int(row["rank"]), float(row["score"])), reverse=True)
    return winner, comparisons


def url_provenance(
    requested_url: str,
    *,
    fetched_url: str | None = None,
    canonical_url: str | None = None,
) -> dict[str, object]:
    requested = requested_url.strip()
    fetched = (fetched_url or requested).strip()
    canonical = (
        canonical_url.strip() if isinstance(canonical_url, str) and canonical_url.strip() else None
    )
    return {
        "requested_url": requested,
        "fetched_url": fetched,
        "canonical_url": canonical,
        "redirected": fetched != requested,
        "canonicalized": canonical is not None and canonical != fetched,
        "requested_domain": _domain(requested),
        "fetched_domain": _domain(fetched),
        "canonical_domain": _domain(canonical) if canonical else None,
    }


def resolve_provenance_url(raw_url: str, *, base_url: str) -> str:
    return urljoin(base_url, raw_url.strip())


def _domain(value: str) -> str:
    return (urlparse(value).hostname or "").lower().removeprefix("www.")
