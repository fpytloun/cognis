"""Add stable normalized generation identity and current-file totals.

Revision ID: 132_work_derived_file_state
Revises: 131_work_cache_recovery_topology
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "132_work_derived_file_state"
down_revision = "131_work_cache_recovery_topology"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    record_file_columns = {column["name"] for column in inspector.get_columns("work_record_files")}
    record_file_additions: dict[str, sa.Column[Any]] = {
        "owner_email": sa.Column("owner_email", sa.String(), nullable=True),
        "session_id": sa.Column("session_id", sa.String(), nullable=True),
        "materializer_version": sa.Column("materializer_version", sa.String(), nullable=True),
        "path_generation_id": sa.Column(
            "path_generation_id", sa.String(), server_default="", nullable=False
        ),
        "source_seq": sa.Column("source_seq", sa.BigInteger(), server_default="0", nullable=False),
        "item_ordinal": sa.Column("item_ordinal", sa.Integer(), server_default="0", nullable=False),
    }
    missing_record_file_columns: list[sa.Column[Any]] = [
        column for name, column in record_file_additions.items() if name not in record_file_columns
    ]
    if missing_record_file_columns:
        with op.batch_alter_table("work_record_files") as batch:
            for column in missing_record_file_columns:
                batch.add_column(column)
    op.execute(
        """
        UPDATE work_record_files
        SET owner_email = (
            SELECT owner_email FROM work_records
            WHERE work_records.work_record_id = work_record_files.work_record_id
        ), session_id = (
            SELECT session_id FROM work_records
            WHERE work_records.work_record_id = work_record_files.work_record_id
        ), materializer_version = (
            SELECT materializer_version FROM work_records
            WHERE work_records.work_record_id = work_record_files.work_record_id
        ), source_seq = (
            SELECT source_seq FROM work_records
            WHERE work_records.work_record_id = work_record_files.work_record_id
        ), item_ordinal = (
            SELECT item_ordinal FROM work_records
            WHERE work_records.work_record_id = work_record_files.work_record_id
        )
        """
    )
    record_file_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("work_record_files")
    }
    with op.batch_alter_table("work_record_files") as batch:
        batch.alter_column("owner_email", nullable=False)
        batch.alter_column("session_id", nullable=False)
        batch.alter_column("materializer_version", nullable=False)
        if "ix_work_record_files_generation_source" not in record_file_indexes:
            batch.create_index(
                "ix_work_record_files_generation_source",
                [
                    "owner_email",
                    "session_id",
                    "materializer_version",
                    "path_generation_id",
                    "source_seq",
                    "item_ordinal",
                    "file_ordinal",
                ],
            )
    current_file_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("work_current_files")
    }
    missing_current_file_columns = [
        sa.Column(name, sa.BigInteger(), server_default="0", nullable=False)
        for name in ("additions", "deletions")
        if name not in current_file_columns
    ]
    with op.batch_alter_table("work_current_files") as batch:
        for column in missing_current_file_columns:
            batch.add_column(column)
        batch.drop_constraint(
            "ck_work_current_files_nonnegative",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_work_current_files_nonnegative",
            "source_seq >= 0 AND rename_ordinal >= 0 AND recreate_ordinal >= 0 "
            "AND preview_size >= 0 AND additions >= 0 AND deletions >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("work_current_files") as batch:
        batch.drop_constraint(
            "ck_work_current_files_nonnegative",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_work_current_files_nonnegative",
            "source_seq >= 0 AND rename_ordinal >= 0 AND recreate_ordinal >= 0 "
            "AND preview_size >= 0",
        )
        batch.drop_column("deletions")
        batch.drop_column("additions")
    with op.batch_alter_table("work_record_files") as batch:
        batch.drop_index("ix_work_record_files_generation_source")
        batch.drop_column("item_ordinal")
        batch.drop_column("source_seq")
        batch.drop_column("path_generation_id")
        batch.drop_column("materializer_version")
        batch.drop_column("session_id")
        batch.drop_column("owner_email")
