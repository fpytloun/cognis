"""Index newest Work call-state reads.

Revision ID: 139_work_call_state_index
Revises: 138_schedule_task_failure_options
"""

from __future__ import annotations

import time

import sqlalchemy as sa
from alembic import op

revision = "139_work_call_state_index"
down_revision = "138_schedule_task_failure_options"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_work_records_owner_version_call_state"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        _upgrade_locked(bind)
        return
    lock_key = "hashtextextended('cognis:work_call_state_index:v1', 0)"
    while not bool(bind.scalar(sa.text(f"SELECT pg_try_advisory_lock({lock_key})"))):
        time.sleep(0.1)
    try:
        _upgrade_locked(bind)
    finally:
        bind.execute(sa.text(f"SELECT pg_advisory_unlock({lock_key})"))


def _upgrade_locked(bind: sa.Connection) -> None:
    existing = next(
        (
            index
            for index in sa.inspect(bind).get_indexes("work_records")
            if index.get("name") == INDEX_NAME
        ),
        None,
    )
    if existing is not None:
        columns_match = existing.get("column_names") == [
            "owner_email",
            "materializer_version",
            "materialized_at",
        ]
        sorting = dict(existing.get("column_sorting") or {})
        descending_matches = "desc" in sorting.get("materialized_at", ())
        options = dict(existing.get("dialect_options") or {})
        predicate_value = options.get(f"{bind.dialect.name}_where")
        predicate = str(predicate_value).lower() if predicate_value is not None else ""
        predicate_matches = "call_id is not null" in predicate
        include_matches = bind.dialect.name != "postgresql" or existing.get("include_columns") == [
            "session_id",
            "call_id",
            "is_evidence",
        ]
        if columns_match and descending_matches and predicate_matches and include_matches:
            return
    if bind.dialect.name == "postgresql":
        replacement_name = f"{INDEX_NAME}_replacement"
        context = op.get_context()
        with context.autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {replacement_name}"))
            target_name = INDEX_NAME
            if existing is not None:
                target_name = replacement_name
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {target_name} "
                    "ON work_records (owner_email, materializer_version, materialized_at DESC) "
                    "INCLUDE (session_id, call_id, is_evidence) WHERE call_id IS NOT NULL"
                )
            )
            if existing is not None:
                op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
                op.execute(sa.text(f"ALTER INDEX {replacement_name} RENAME TO {INDEX_NAME}"))
        return
    if existing is not None:
        op.drop_index(INDEX_NAME, table_name="work_records")
    op.create_index(
        INDEX_NAME,
        "work_records",
        [
            "owner_email",
            "materializer_version",
            sa.text("materialized_at DESC"),
        ],
        unique=False,
        postgresql_include=["session_id", "call_id", "is_evidence"],
        postgresql_where=sa.text("call_id IS NOT NULL"),
        sqlite_where=sa.text("call_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="work_records")
