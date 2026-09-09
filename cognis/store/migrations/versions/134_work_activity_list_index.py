"""Add the owner activity-list session index.

Revision ID: 134_work_activity_list_index
Revises: 133_work_generation_snapshots
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "134_work_activity_list_index"
down_revision = "133_work_generation_snapshots"
branch_labels = None
depends_on = None

_INDEX = "ix_sessions_owner_activity_scope_updated"
_TABLE = "sessions"
_COLUMNS = ("user_email", "activity_scope_id", "updated_at", "session_id")


def _postgresql_index(bind: sa.Connection) -> sa.RowMapping | None:
    return (
        bind.execute(
            sa.text(
                """
                SELECT
                    index_metadata.indisvalid AS valid,
                    table_relation.relname AS table_name,
                    index_metadata.indisunique AS is_unique,
                    index_metadata.indpred IS NULL AS has_no_predicate,
                    index_metadata.indexprs IS NULL AS has_no_expressions,
                    access_method.amname AS access_method,
                    index_metadata.indnkeyatts AS key_count,
                    index_metadata.indnatts AS total_count,
                    array_agg(attribute.attname ORDER BY key.ordinality)
                        FILTER (
                            WHERE key.ordinality <= index_metadata.indnkeyatts
                        ) AS columns,
                    pg_get_indexdef(index_relation.oid) AS definition
                FROM pg_class AS index_relation
                JOIN pg_namespace AS namespace
                    ON namespace.oid = index_relation.relnamespace
                JOIN pg_index AS index_metadata
                    ON index_metadata.indexrelid = index_relation.oid
                JOIN pg_class AS table_relation
                    ON table_relation.oid = index_metadata.indrelid
                JOIN pg_am AS access_method
                    ON access_method.oid = index_relation.relam
                JOIN LATERAL unnest(index_metadata.indkey)
                    WITH ORDINALITY AS key(attnum, ordinality) ON TRUE
                LEFT JOIN pg_attribute AS attribute
                    ON attribute.attrelid = index_metadata.indrelid
                    AND attribute.attnum = key.attnum
                WHERE
                    namespace.nspname = current_schema()
                    AND index_relation.relname = :name
                GROUP BY
                    index_metadata.indisvalid,
                    table_relation.relname,
                    index_metadata.indisunique,
                    index_metadata.indpred IS NULL,
                    index_metadata.indexprs IS NULL,
                    access_method.amname,
                    index_metadata.indnkeyatts,
                    index_metadata.indnatts,
                    pg_get_indexdef(index_relation.oid)
                """
            ),
            {"name": _INDEX},
        )
        .mappings()
        .one_or_none()
    )


def _is_exact_postgresql_index(row: sa.RowMapping) -> bool:
    expected_suffix = f"USING btree ({', '.join(_COLUMNS)})"
    return bool(
        row["table_name"] == _TABLE
        and tuple(row["columns"] or ()) == _COLUMNS
        and str(row["definition"]).endswith(expected_suffix)
        and not row["is_unique"]
        and row["has_no_predicate"]
        and row["has_no_expressions"]
        and row["access_method"] == "btree"
        and row["key_count"] == len(_COLUMNS)
        and row["total_count"] == len(_COLUMNS)
    )


def upgrade() -> None:
    bind = op.get_bind()
    column_sql = ", ".join(_COLUMNS)
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            existing = _postgresql_index(bind)
            if existing is not None:
                if existing["valid"] and _is_exact_postgresql_index(existing):
                    return
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} ({column_sql})"
                )
            )
        return

    reflected_index = next(
        (index for index in sa.inspect(bind).get_indexes(_TABLE) if index["name"] == _INDEX),
        None,
    )
    sqlite_where = (
        (reflected_index.get("dialect_options") or {}).get("sqlite_where")
        if reflected_index is not None
        else None
    )
    if reflected_index is not None and (
        tuple(reflected_index["column_names"]) != _COLUMNS
        or bool(reflected_index.get("unique"))
        or sqlite_where is not None
    ):
        op.drop_index(_INDEX, table_name=_TABLE)
        reflected_index = None
    if reflected_index is None:
        op.create_index(_INDEX, _TABLE, list(_COLUMNS))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        return
    existing = {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
