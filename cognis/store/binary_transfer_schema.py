"""Idempotent metadata-only schema for generic binary transfers."""

from __future__ import annotations

from typing import cast

from sqlalchemy import BigInteger, Table, inspect, text
from sqlalchemy.engine import Connection

from cognis.store.models import BinaryTransferRow, BinaryTransferScopeRow


def ensure_binary_transfer_schema(connection: Connection) -> None:
    """Create metadata tables and preserve large sizes on both supported databases."""
    inspector = inspect(connection)
    if connection.dialect.name == "postgresql" and inspector.has_table("artifacts"):
        size = next(
            column
            for column in inspector.get_columns("artifacts")
            if column["name"] == "size_bytes"
        )
        if not isinstance(size["type"], BigInteger):
            connection.execute(text("ALTER TABLE artifacts ALTER COLUMN size_bytes TYPE BIGINT"))
    # SQLite INTEGER storage is already signed 64-bit; no table rewrite is needed.
    for model in (BinaryTransferScopeRow, BinaryTransferRow):
        cast(Table, model.__table__).create(connection, checkfirst=True)
