"""Idempotent schema additions for terminal task handling."""

from __future__ import annotations

from sqlalchemy import Connection, inspect


def ensure_task_outcome_schema(connection: Connection) -> None:
    """Add optional owner instructions without changing existing tasks."""
    inspector = inspect(connection)
    for table in ("tasks", "schedules"):
        if table in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns(table)}
            if "on_finish_instructions" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN on_finish_instructions TEXT"
                )
