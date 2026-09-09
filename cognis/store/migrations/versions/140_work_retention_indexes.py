"""Index Work source-content retention.

Revision ID: 140_work_retention_indexes
Revises: 139_work_call_state_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "140_work_retention_indexes"
down_revision = "139_work_call_state_index"
branch_labels = None
depends_on = None

RECORD_INDEX = "ix_work_records_source_retention"
FILE_INDEX = "ix_work_current_files_content_retention"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    record_indexes = {
        str(index["name"]) for index in inspector.get_indexes("work_records") if index.get("name")
    }
    file_indexes = {
        str(index["name"])
        for index in inspector.get_indexes("work_current_files")
        if index.get("name")
    }
    if RECORD_INDEX in record_indexes and FILE_INDEX in file_indexes:
        return
    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {RECORD_INDEX}"))
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {FILE_INDEX}"))
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {RECORD_INDEX} "
                    "ON work_records (materializer_version, materialized_at, work_record_id) "
                    "WHERE source_content_scrubbed_at IS NULL"
                )
            )
            op.execute(
                sa.text(
                    f"CREATE INDEX CONCURRENTLY {FILE_INDEX} "
                    "ON work_current_files (content_expires_at, current_file_id) "
                    "WHERE content_scrubbed_at IS NULL"
                )
            )
        return
    if RECORD_INDEX not in record_indexes:
        op.create_index(
            RECORD_INDEX,
            "work_records",
            ["materializer_version", "materialized_at", "work_record_id"],
            sqlite_where=sa.text("source_content_scrubbed_at IS NULL"),
        )
    if FILE_INDEX not in file_indexes:
        op.create_index(
            FILE_INDEX,
            "work_current_files",
            ["content_expires_at", "current_file_id"],
            sqlite_where=sa.text("content_scrubbed_at IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        context = op.get_context()
        with context.autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {FILE_INDEX}"))
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {RECORD_INDEX}"))
        return
    op.drop_index(FILE_INDEX, table_name="work_current_files")
    op.drop_index(RECORD_INDEX, table_name="work_records")
