"""Small portable batch primitives shared by accounting background owners."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import Table, text
from sqlalchemy.dialects.postgresql import Insert as PostgreSQLInsert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import Insert as SQLiteInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import FromClause

USAGE_SIGNAL_KIND = "usage_changed"


def insert_for(session: AsyncSession, table: FromClause) -> PostgreSQLInsert | SQLiteInsert:
    """Both supported databases provide atomic conflict handling."""
    if not isinstance(table, Table):
        raise TypeError("Usage writes require a mapped table")
    return (
        postgresql_insert(table)
        if session.get_bind().dialect.name == "postgresql"
        else sqlite_insert(table)
    )


async def sqlite_write_boundary(session: AsyncSession) -> None:
    """Avoid a deferred-read snapshot upgrade race on the SQLite test/development DB."""
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(text("BEGIN IMMEDIATE"))


async def notify_usage(
    session: AsyncSession,
    controller_id: str,
    revision: str,
    *,
    kind: Literal["usage_changed", "usage_pricing_changed"] = "usage_changed",
) -> None:
    """Intent and NOTIFY commit atomically; payload is identity-free control metadata."""
    if session.get_bind().dialect.name == "postgresql":
        import json

        payload = json.dumps(
            {
                "version": 1,
                "kind": kind,
                "origin_controller_id": controller_id,
                "scope": {},
                "revision": revision,
            },
            separators=(",", ":"),
        )
        await session.execute(
            text("SELECT pg_notify('cognis_cluster_v1', :payload)"), {"payload": payload}
        )


def row_values(row: Any) -> dict[str, Any]:
    """Extract mapped scalar columns, not SQLAlchemy identity/cache state."""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
