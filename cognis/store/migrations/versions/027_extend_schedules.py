"""Extend schedules table with schedule types, error tracking, and heartbeat support.

Revision ID: 027
Revises: 026
Create Date: 2026-04-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026_executor_runtime_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        # Schedule type support (cron, interval, one_shot)
        batch_op.add_column(
            sa.Column("description", sa.Text, nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "schedule_type",
                sa.String,
                nullable=False,
                server_default="cron",
            ),
        )
        batch_op.add_column(
            sa.Column("interval_seconds", sa.Integer, nullable=True),
        )
        batch_op.add_column(
            sa.Column("one_shot_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
        batch_op.add_column(
            sa.Column("timezone", sa.String, nullable=False, server_default="UTC"),
        )
        # Concurrency and lifecycle
        batch_op.add_column(
            sa.Column(
                "max_concurrent_runs",
                sa.Integer,
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        batch_op.add_column(
            sa.Column(
                "delete_after_run",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            ),
        )
        # Error tracking
        batch_op.add_column(
            sa.Column("last_run_status", sa.String, nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "consecutive_errors",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
        batch_op.add_column(
            sa.Column("disabled_reason", sa.Text, nullable=True),
        )
        # Heartbeat smart suppression
        batch_op.add_column(
            sa.Column(
                "suppress_empty",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            ),
        )
        # Make cron_expr nullable (only required for cron type)
        batch_op.alter_column("cron_expr", existing_type=sa.String, nullable=True)

    # Index for the scheduler timer loop query
    op.create_index(
        "ix_schedules_enabled_next_fire",
        "schedules",
        ["enabled", "next_fire_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_schedules_enabled_next_fire", table_name="schedules")

    with op.batch_alter_table("schedules") as batch_op:
        batch_op.alter_column("cron_expr", existing_type=sa.String, nullable=False)
        batch_op.drop_column("suppress_empty")
        batch_op.drop_column("disabled_reason")
        batch_op.drop_column("consecutive_errors")
        batch_op.drop_column("last_run_status")
        batch_op.drop_column("delete_after_run")
        batch_op.drop_column("max_concurrent_runs")
        batch_op.drop_column("timezone")
        batch_op.drop_column("one_shot_at")
        batch_op.drop_column("interval_seconds")
        batch_op.drop_column("schedule_type")
        batch_op.drop_column("description")
