"""Idempotent document permissions and revision attribution schema."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection


def ensure_document_write_schema(connection: Connection) -> None:
    """Add write permissions without upgrading any existing read grant."""
    tables = set(sa.inspect(connection).get_table_names())
    if "artifact_grants" in tables:
        columns = {c["name"] for c in sa.inspect(connection).get_columns("artifact_grants")}
        if "permission" not in columns:
            connection.execute(
                sa.text(
                    "ALTER TABLE artifact_grants ADD COLUMN permission VARCHAR NOT NULL DEFAULT 'read'"
                )
            )
    for table in ("artifacts", "deliverables"):
        if table not in tables:
            continue
        columns = {c["name"] for c in sa.inspect(connection).get_columns(table)}
        # Backfill only when introducing attribution. A later bootstrap must not
        # reinterpret a human author (null agent) as the lineage's owning agent.
        if "author_email" not in columns:
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN author_email VARCHAR"))
            if "owner_email" in columns:
                connection.execute(sa.text(f"UPDATE {table} SET author_email = owner_email"))
        if "author_agent_id" not in columns:
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN author_agent_id VARCHAR"))
            if {"session_id", "owner_email"} <= columns and "sessions" in tables:
                connection.execute(
                    sa.text(
                        f"UPDATE {table} SET author_agent_id = "
                        f"(SELECT s.agent_id FROM sessions s WHERE s.session_id = {table}.session_id "
                        f"AND s.user_email = {table}.owner_email)"
                        + (" WHERE message_role = 'assistant'" if table == "artifacts" else "")
                    )
                )


def rollback_document_write_schema(connection: Connection) -> None:
    """Remove only this migration's columns."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ops = Operations(MigrationContext.configure(connection))
    for table, names in (
        ("artifact_grants", ("permission",)),
        ("artifacts", ("author_email", "author_agent_id")),
        ("deliverables", ("author_email", "author_agent_id")),
    ):
        if table not in sa.inspect(connection).get_table_names():
            continue
        columns = {c["name"] for c in sa.inspect(connection).get_columns(table)}
        with ops.batch_alter_table(table) as batch:
            for name in names:
                if name in columns:
                    batch.drop_column(name)
