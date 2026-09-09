"""Persist normalized Work file state metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "129_work_record_file_metadata"
down_revision: str | Sequence[str] | None = "128_work_load_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("work_record_files")
    }
    additions = {
        "status": sa.Column("status", sa.String(), nullable=True),
        "old_path": sa.Column("old_path", sa.Text(), nullable=True),
        "old_path_id": sa.Column("old_path_id", sa.String(), nullable=True),
        "binary": sa.Column("binary", sa.Boolean(), server_default=sa.false(), nullable=False),
        "generated": sa.Column(
            "generated", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        "truncated": sa.Column(
            "truncated", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        "preview_omitted": sa.Column(
            "preview_omitted", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("work_record_files", column)
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("work_record_files")
    }
    if "ix_work_record_files_old_path" not in indexes:
        op.create_index(
            "ix_work_record_files_old_path",
            "work_record_files",
            ["old_path_id", "work_record_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("work_record_files")}
    if "ix_work_record_files_old_path" in indexes:
        op.drop_index("ix_work_record_files_old_path", table_name="work_record_files")
    columns = {column["name"] for column in inspector.get_columns("work_record_files")}
    for name in (
        "preview_omitted",
        "truncated",
        "generated",
        "binary",
        "old_path_id",
        "old_path",
        "status",
    ):
        if name in columns:
            op.drop_column("work_record_files", name)
