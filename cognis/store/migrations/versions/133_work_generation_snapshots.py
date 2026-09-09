"""Add immutable Work generation snapshots and invalidation outbox.

Revision ID: 133_work_generation_snapshots
Revises: 132_work_derived_file_state
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "133_work_generation_snapshots"
down_revision = "132_work_derived_file_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    record_columns: dict[str, Any] = {
        column["name"]: column for column in sa.inspect(bind).get_columns("work_records")
    }
    missing_record_columns = [
        sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=True)
        for name in ("source_content_expires_at", "source_content_scrubbed_at")
        if name not in record_columns
    ]
    if missing_record_columns:
        with op.batch_alter_table("work_records") as batch:
            for column in missing_record_columns:
                batch.add_column(column)
    if bind.dialect.name == "postgresql":
        for name in ("source_content_expires_at", "source_content_scrubbed_at"):
            column_type = record_columns.get(name, {}).get("type")
            if column_type is not None and not bool(getattr(column_type, "timezone", False)):
                op.alter_column(
                    "work_records",
                    name,
                    existing_type=column_type,
                    type_=sa.TIMESTAMP(timezone=True),
                    postgresql_using=f"{name} AT TIME ZONE 'UTC'",
                )

    op.create_table(
        "work_generation_summary_snapshots",
        sa.Column("scope_key", sa.String(), primary_key=True),
        sa.Column("cache_epoch", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("materializer_version", sa.String(), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.Column("covered_through_seq", sa.BigInteger(), nullable=False),
        sa.Column("record_count", sa.BigInteger(), nullable=False),
        sa.Column("evidence_record_count", sa.BigInteger(), nullable=False),
        sa.Column("mutation_count", sa.Integer(), nullable=False),
        sa.Column("command_count", sa.Integer(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("artifact_count", sa.Integer(), nullable=False),
        sa.Column("deliverable_count", sa.Integer(), nullable=False),
        sa.Column("additions", sa.BigInteger(), nullable=False),
        sa.Column("deletions", sa.BigInteger(), nullable=False),
        sa.Column("omitted_file_count", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_key", "cache_epoch"],
            ["work_scope_generations.scope_key", "work_scope_generations.cache_epoch"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_work_generation_summary_snapshots_order",
        "work_generation_summary_snapshots",
        ["scope_key", "cache_epoch", "member_ordinal", "session_id"],
    )
    op.create_table(
        "work_generation_current_file_snapshots",
        sa.Column("scope_key", sa.String(), primary_key=True),
        sa.Column("cache_epoch", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("path_generation_id", sa.String(), primary_key=True),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("materializer_version", sa.String(), nullable=False),
        sa.Column("file_projector_version", sa.String(), nullable=False),
        sa.Column("member_ordinal", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_key", "cache_epoch"],
            ["work_scope_generations.scope_key", "work_scope_generations.cache_epoch"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_work_generation_current_file_snapshots_order",
        "work_generation_current_file_snapshots",
        [
            "scope_key",
            "cache_epoch",
            "member_ordinal",
            "session_id",
            "path_generation_id",
        ],
    )
    op.create_table(
        "work_invalidation_outbox",
        sa.Column("outbox_id", sa.String(), primary_key=True),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("cache_epoch", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_work_invalidation_outbox_retry",
        "work_invalidation_outbox",
        ["next_retry_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("work_invalidation_outbox")
    op.drop_table("work_generation_current_file_snapshots")
    op.drop_table("work_generation_summary_snapshots")
    with op.batch_alter_table("work_records") as batch:
        batch.drop_column("source_content_scrubbed_at")
        batch.drop_column("source_content_expires_at")
