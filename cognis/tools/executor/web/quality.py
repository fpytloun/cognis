"""Shared semantic quality checks for extracted web pages."""

from __future__ import annotations

import re
from typing import Any

_PROVIDER_ERROR_BODY_MARKERS = (
    "something went wrong on our end",
    "we ran into a problem",
    "please go back and try again",
    "temporarily unavailable",
)


def classify_provider_error_page(document: dict[str, Any], content: str) -> str | None:
    """Identify short provider-generated error documents returned with usable HTML."""
    title = _normalize(str(document.get("title") or ""))
    body = _normalize(content)
    title_signal = (
        title == "error page"
        or title.startswith("error page ")
        or title.endswith(" error page")
        or title in {"oops", "sorry"}
    )
    body_signal_count = sum(marker in body for marker in _PROVIDER_ERROR_BODY_MARKERS)
    if body_signal_count and title_signal:
        return "provider_error_page"
    if body_signal_count >= 2 and len(body) < 2_000:
        return "provider_error_page"
    return None


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())
