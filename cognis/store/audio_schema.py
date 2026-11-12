"""Idempotent schema upgrades for durable audio metadata."""

from sqlalchemy import Connection, inspect


def ensure_audio_job_plans(connection: Connection) -> None:
    """Keep legacy jobs unchanged while persisting immutable relative-length plans."""
    inspector = inspect(connection)
    if "audio_jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("audio_jobs")}
    for name, sql_type in (("length_preference", "VARCHAR(16)"), ("target_words", "INTEGER")):
        if name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE audio_jobs ADD COLUMN {name} {sql_type}")


def ensure_audio_job_warnings(connection: Connection) -> None:
    """Preserve voice-fallback warnings across polling and controller changes."""
    inspector = inspect(connection)
    if "audio_jobs" not in inspector.get_table_names():
        return
    if "warnings" not in {column["name"] for column in inspector.get_columns("audio_jobs")}:
        connection.exec_driver_sql(
            "ALTER TABLE audio_jobs ADD COLUMN warnings JSON NOT NULL DEFAULT '[]'"
        )
