"""Provider-neutral web search intent helpers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

SEARCH_MODES = frozenset({"web", "news", "images", "videos"})
RESULT_TYPES = frozenset({"paper", "repository", "discussion", "document"})


def search_mode(options: dict[str, Any]) -> str:
    """Return the validated provider-neutral search mode."""
    value = str(options.get("search_mode") or "web").strip().lower()
    return value if value in SEARCH_MODES else "web"


def result_type(options: dict[str, Any]) -> str | None:
    """Return the validated optional semantic result preference."""
    raw = options.get("result_type")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in RESULT_TYPES else None


def intent_metadata(
    *,
    requested_mode: str,
    effective_mode: str,
    preferred_result_type: str | None,
    native_mode_support: bool,
    degraded_reason: str | None = None,
) -> dict[str, object]:
    """Build consistent intent/effective-mode metadata across backends."""
    degraded = requested_mode != effective_mode or not native_mode_support
    metadata: dict[str, object] = {
        "requested_search_mode": requested_mode,
        "effective_search_mode": effective_mode,
        "native_mode_support": native_mode_support,
        "preferred_result_type": preferred_result_type,
        "search_degraded": degraded,
    }
    if degraded_reason:
        metadata["degraded_reason"] = degraded_reason
    return metadata


def semantic_score(preferred_type: str | None, url: str, title: str) -> float:
    if preferred_type is None:
        return 0.0
    value = f"{url} {title}".lower()
    patterns = {
        "paper": r"(?:arxiv\.org|pubmed\.ncbi|doi\.org|research paper)",
        "repository": r"(?:github\.com|gitlab\.com|codeberg\.org)",
        "discussion": r"(?:reddit\.com|stackoverflow\.com|/forum|/discussion|/questions/)",
        "document": r"(?:\.pdf(?:\?|$)|/docs?/|/documentation/|/manuals?/)",
    }
    return 2.0 if re.search(patterns[preferred_type], value) else 0.0


def domain_allowed(url: str, options: dict[str, Any]) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    include = _domains(options.get("include_domains"))
    exclude = _domains(options.get("exclude_domains"))

    def matches(domain: str) -> bool:
        return host == domain or host.endswith(f".{domain}")

    return not any(matches(domain) for domain in exclude) and (
        not include or any(matches(domain) for domain in include)
    )


def _domains(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).lower().removeprefix("www.") for item in value if str(item)}
