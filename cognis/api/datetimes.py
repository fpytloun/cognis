"""UTC normalization for Cognis metadata at API boundaries."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_datetime(value: datetime) -> datetime:
    """Restore UTC offsets lost when SQLite reloads metadata timestamps."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
