"""Helpers for safe client-facing error details.

These helpers are intentionally conservative. If a detail string cannot be
cleanly reduced to a short, obviously safe fragment, callers should fall back
to a category-only message.
"""

from __future__ import annotations

import re

_API_KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"key-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(api[_ -]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"https?://[^\s:@]+:[^\s@]+@"),
]
_LONG_QUOTED_CONTENT = re.compile(r'(["\'])([^"\']{51,})\1')
_WHITESPACE = re.compile(r"\s+")
_MAX_DETAIL_LENGTH = 200


def sanitize_client_error_detail(value: str | Exception | None, *, fallback: str) -> str:
    """Return a short client-safe error detail string."""

    if value is None:
        return fallback

    message = str(value)
    for pattern in _API_KEY_PATTERNS:
        message = pattern.sub(r"\1[redacted]" if pattern.groups else "[redacted]", message)
    message = _LONG_QUOTED_CONTENT.sub("[redacted-content]", message)
    message = _WHITESPACE.sub(" ", message).strip()
    if not message:
        return fallback
    if len(message) > _MAX_DETAIL_LENGTH:
        message = message[: _MAX_DETAIL_LENGTH - 3].rstrip() + "..."
    return message
