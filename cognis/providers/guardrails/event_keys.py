"""Session-scoped keys for Intaris event append idempotency."""

from __future__ import annotations

import hashlib
import json
import re

_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:@-]+\Z")


class EventIdempotencyKeyError(ValueError):
    """A key does not satisfy the receiving session event-store contract."""


def validate_event_idempotency_key(session_id: str, key: str) -> None:
    """Reject invalid keys before an authenticated append is attempted."""
    if (
        not session_id
        or not key.startswith(f"{session_id}:")
        or len(key) > 256
        or not _KEY_PATTERN.fullmatch(key)
    ):
        raise EventIdempotencyKeyError("Invalid session-scoped event idempotency key")


def event_idempotency_key(session_id: str, purpose: str, *parts: str | int) -> str:
    """Build a bounded key satisfying the receiving event-store contract."""
    digest = hashlib.sha256(
        json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
    key = f"{session_id}:{purpose}:{digest}"
    validate_event_idempotency_key(session_id, key)
    return key
