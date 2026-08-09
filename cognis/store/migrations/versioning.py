from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Connection

ALEMBIC_VERSION_NUM_LENGTH = 255


def ensure_alembic_version_capacity(connection: Connection) -> None:
    """Expand PostgreSQL's Alembic revision column before long revision writes."""
    if connection.dialect.name != "postgresql":
        return

    version_column = next(
        column
        for column in sa.inspect(connection).get_columns("alembic_version")
        if column["name"] == "version_num"
    )
    current_length = getattr(version_column["type"], "length", None)
    if current_length is not None and current_length >= ALEMBIC_VERSION_NUM_LENGTH:
        return

    connection.execute(
        sa.text(
            "ALTER TABLE alembic_version "
            f"ALTER COLUMN version_num TYPE VARCHAR({ALEMBIC_VERSION_NUM_LENGTH})"
        )
    )
