"""Persist authoritative Work record categories and summary metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "127_work_record_categories"
down_revision: str | Sequence[str] | None = "126_task_delivery_preferred_channel_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("work_records")}
    additions = {
        "category": sa.Column("category", sa.String(), nullable=True),
        "entity_id": sa.Column("entity_id", sa.String(), nullable=True),
        "file_path_ids": sa.Column("file_path_ids", sa.JSON(), server_default="[]", nullable=False),
        "additions": sa.Column("additions", sa.Integer(), server_default="0", nullable=False),
        "deletions": sa.Column("deletions", sa.Integer(), server_default="0", nullable=False),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("work_records", column)
    indexes = {index["name"] for index in inspector.get_indexes("work_records")}
    if "ix_work_records_owner_category_order" not in indexes:
        op.create_index(
            "ix_work_records_owner_category_order",
            "work_records",
            [
                "owner_email",
                "materializer_version",
                "is_evidence",
                "category",
                "occurred_at",
                "session_id",
                "source_seq",
                "item_ordinal",
                "work_record_id",
            ],
        )
    if "ix_work_records_owner_category_entity" not in indexes:
        op.create_index(
            "ix_work_records_owner_category_entity",
            "work_records",
            ["owner_email", "materializer_version", "category", "entity_id"],
        )
    if "work_record_files" not in inspector.get_table_names():
        op.create_table(
            "work_record_files",
            sa.Column("work_record_file_id", sa.String(), nullable=False),
            sa.Column("work_record_id", sa.String(), nullable=False),
            sa.Column("file_ordinal", sa.Integer(), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("path_id", sa.String(), nullable=False),
            sa.Column("additions", sa.Integer(), nullable=False),
            sa.Column("deletions", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["work_record_id"],
                ["work_records.work_record_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("work_record_file_id"),
            sa.UniqueConstraint(
                "work_record_id",
                "file_ordinal",
                name="uq_work_record_files_record_ordinal",
            ),
        )
        op.create_index(
            "ix_work_record_files_record_order",
            "work_record_files",
            ["work_record_id", "file_ordinal"],
        )
        op.create_index(
            "ix_work_record_files_path",
            "work_record_files",
            ["path_id", "work_record_id"],
        )


def downgrade() -> None:
    op.drop_table("work_record_files")
    op.drop_index("ix_work_records_owner_category_entity", table_name="work_records")
    op.drop_index("ix_work_records_owner_category_order", table_name="work_records")
    op.drop_column("work_records", "deletions")
    op.drop_column("work_records", "additions")
    op.drop_column("work_records", "file_path_ids")
    op.drop_column("work_records", "entity_id")
    op.drop_column("work_records", "category")
