"""Codify production Work graph lookup indexes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "128_work_load_indexes"
down_revision: str | Sequence[str] | None = "127_work_record_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    (
        "ix_sessions_owner_conversation_activity_scope_session",
        "sessions",
        ("user_email", "conversation_id", "activity_scope_id", "session_id"),
    ),
    (
        "ix_step_runs_task_step_run",
        "step_runs",
        ("task_id", "step_run_id"),
    ),
    (
        "ix_step_runs_session_status",
        "step_runs",
        ("session_id", "status"),
    ),
)


def _postgresql_index(bind: sa.Connection, name: str) -> sa.RowMapping | None:
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
            {"name": name},
        )
        .mappings()
        .one_or_none()
    )


def _is_exact_index(
    row: sa.RowMapping,
    *,
    table: str,
    columns: tuple[str, ...],
) -> bool:
    expected_suffix = f"USING btree ({', '.join(columns)})"
    return bool(
        row["table_name"] == table
        and tuple(row["columns"] or ()) == columns
        and str(row["definition"]).endswith(expected_suffix)
        and not row["is_unique"]
        and row["has_no_predicate"]
        and row["has_no_expressions"]
        and row["access_method"] == "btree"
        and row["key_count"] == len(columns)
        and row["total_count"] == len(columns)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, table, columns in _INDEXES:
                existing = _postgresql_index(bind, name)
                if existing is not None:
                    if not _is_exact_index(existing, table=table, columns=columns):
                        raise RuntimeError(f"Conflicting PostgreSQL index definition for {name}")
                    if existing["valid"]:
                        continue
                    op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
                column_sql = ", ".join(columns)
                op.execute(
                    sa.text(
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column_sql})"
                    )
                )
        return

    for name, table, columns in _INDEXES:
        existing_names = {
            str(index["name"]) for index in sa.inspect(bind).get_indexes(table) if index.get("name")
        }
        if name not in existing_names:
            op.create_index(name, table, list(columns))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _table, _columns in reversed(_INDEXES):
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        return

    for name, table, _columns in reversed(_INDEXES):
        existing = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)
