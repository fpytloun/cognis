"""Durable reauthorization state for controller-managed provider OAuth tokens.

A refresh token rejected by its token endpoint never becomes valid again. The
rejection is recorded as a marker inside the encrypted token record, so status
reads and later requests report reauthorization without contacting the token
endpoint again. Completing the provider OAuth flow writes a new record without
the marker, which is the only way to clear it.
"""

from __future__ import annotations

import time

from cognis import json_codec as json

# Token-endpoint statuses that reject the refresh grant itself (invalid_grant,
# revoked or unknown client). Other failures keep their transient handling.
OAUTH_REFRESH_REJECTED_STATUSES = frozenset({400, 401})
REAUTHORIZATION_REQUIRED_MARKER = "reauthorization_required_at"


def reauthorization_required(raw: str) -> bool:
    """Return whether a stored token record carries the rejected-refresh marker."""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get(REAUTHORIZATION_REQUIRED_MARKER) is not None


def mark_reauthorization_required(raw: str) -> str:
    """Return the stored token record with the rejected-refresh marker added."""

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("OAuth token record must be a JSON object")
    payload[REAUTHORIZATION_REQUIRED_MARKER] = time.time()
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)
