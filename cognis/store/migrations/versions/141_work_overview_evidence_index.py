"""Index Work evidence for scoped Activity Overview queries.

Revision ID: 141_work_overview_evidence_index
Revises: 140_work_retention_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "141_work_overview_evidence_index"
down_revision = "140_work_retention_indexes"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_work_records_overview_evidence"


def _postgresql_index_matches(bind: object) -> bool:
    row = bind.execute(  # type: ignore[attr-defined]
        sa.text(
            """
            SELECT pg_get_indexdef(indexrelid) AS index_definition,
                   indisvalid AS is_valid
            FROM pg_index
            WHERE indexrelid = to_regclass(:index_name)
            """
        ),
        {"index_name": INDEX_NAME},
    ).first()
    if row is None or not bool(row.is_valid):
        return False
    definition = " ".join(str(row.index_definition).lower().split())
    return all(
        fragment in definition
        for fragment in (
            "on public.work_records using btree "
            "(owner_email, session_id, materializer_version, category, work_record_id)",
            "include (entity_id)",
            "where (is_evidence is true)",
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        if _postgresql_index_matches(bind):
            return
        context = op.get_context()
        with context.autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
                    "ON work_records "
                    "(owner_email, session_id, materializer_version, category, work_record_id) "
                    "INCLUDE (entity_id) "
                    "WHERE is_evidence IS TRUE"
                )
            )
        return
    indexes = {
        str(index["name"])
        for index in sa.inspect(bind).get_indexes("work_records")
        if index.get("name")
    }
    if INDEX_NAME in indexes:
        return
    op.create_index(
        INDEX_NAME,
        "work_records",
        [
            "owner_email",
            "session_id",
            "materializer_version",
            "category",
            "work_record_id",
        ],
        sqlite_where=sa.text("is_evidence IS TRUE"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
        return
    op.drop_index(INDEX_NAME, table_name="work_records")
