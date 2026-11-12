"""The single additive schema step for opt-in, redacted video error metadata."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


def ensure_video_error_message(connection: Connection) -> None:
    """Add the nullable column without changing existing job or billing data."""
    inspector = inspect(connection)
    if "video_jobs" not in inspector.get_table_names():
        return
    if "provider_error_message" in {
        column["name"] for column in inspector.get_columns("video_jobs")
    }:
        return
    guard = " IF NOT EXISTS" if connection.dialect.name == "postgresql" else ""
    connection.execute(
        text(f"ALTER TABLE video_jobs ADD COLUMN{guard} provider_error_message VARCHAR(512)")
    )
