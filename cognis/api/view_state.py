"""Helpers for authoritative frontend conversation-view reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from cognis import __version__


def server_time_iso() -> str:
    """Return an ISO timestamp suitable for frontend freshness checks."""

    return datetime.now(UTC).isoformat()


def cognis_build_id() -> str:
    """Return the controller/app build identifier exposed to PWA clients."""

    return os.environ.get("COGNIS_BUILD_ID") or os.environ.get("COGNIS_BUILD_SHA") or __version__


def runtime_generation(payload: dict[str, Any]) -> str:
    """Build a stable generation hash for volatile runtime state."""

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def conversation_view_version(
    *,
    active_session_id: str | None,
    active_session_last_seq: int,
    runtime_generation_value: str,
    queued_count: int,
) -> str:
    """Build a compact view version from timeline, runtime, and queue state."""

    session_part = active_session_id or "none"
    return f"{session_part}:{active_session_last_seq}:{runtime_generation_value}:{queued_count}"
