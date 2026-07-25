"""Move deliverable payload bytes to object storage.

Revision ID: 075_deliverable_object_store_payloads
Revises: 074_repair_rich_deliverables_step_run_nullable
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "075_deliverable_object_store_payloads"
down_revision = "074_repair_rich_deliverables_step_run_nullable"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("deliverables")}


def upgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM deliverables")).scalar_one()
    message = f"075_deliverable_object_store_payloads: deleting {count} existing deliverable rows"
    config = op.get_context().config
    if config is not None:
        config.print_stdout(message)
    else:
        print(message)
    bind.execute(sa.text("DELETE FROM deliverables"))

    columns = _column_names()
    with op.batch_alter_table("deliverables") as batch:
        for name in ("rich_payload", "content"):
            if name in columns:
                batch.drop_column(name)
        for name, column in (
            (
                "storage_namespace",
                sa.Column(
                    "storage_namespace", sa.String(), nullable=False, server_default="deliverables"
                ),
            ),
            ("storage_object_id", sa.Column("storage_object_id", sa.String(), nullable=False)),
            (
                "content_key",
                sa.Column("content_key", sa.String(), nullable=False, server_default="content.md"),
            ),
            ("content_mime", sa.Column("content_mime", sa.String(), nullable=False)),
            (
                "content_size",
                sa.Column("content_size", sa.Integer(), nullable=False, server_default="0"),
            ),
            ("content_hash", sa.Column("content_hash", sa.String(), nullable=False)),
            ("rich_key", sa.Column("rich_key", sa.String(), nullable=True)),
            ("rich_size", sa.Column("rich_size", sa.Integer(), nullable=True)),
            ("rich_hash", sa.Column("rich_hash", sa.String(), nullable=True)),
            ("outputs_key", sa.Column("outputs_key", sa.String(), nullable=True)),
            ("outputs_mime", sa.Column("outputs_mime", sa.String(), nullable=True)),
            ("outputs_size", sa.Column("outputs_size", sa.Integer(), nullable=True)),
            ("outputs_hash", sa.Column("outputs_hash", sa.String(), nullable=True)),
            ("html_cache_key", sa.Column("html_cache_key", sa.String(), nullable=True)),
            ("pdf_cache_key", sa.Column("pdf_cache_key", sa.String(), nullable=True)),
        ):
            if name not in columns:
                batch.add_column(column)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM deliverables"))
    columns = _column_names()
    with op.batch_alter_table("deliverables") as batch:
        for name in (
            "pdf_cache_key",
            "html_cache_key",
            "outputs_hash",
            "outputs_size",
            "outputs_mime",
            "outputs_key",
            "rich_hash",
            "rich_size",
            "rich_key",
            "content_hash",
            "content_size",
            "content_mime",
            "content_key",
            "storage_object_id",
            "storage_namespace",
        ):
            if name in columns:
                batch.drop_column(name)
        if "content" not in columns:
            batch.add_column(sa.Column("content", sa.Text(), nullable=False))
        if "rich_payload" not in columns:
            batch.add_column(sa.Column("rich_payload", sa.JSON(), nullable=True))
