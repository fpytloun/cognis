"""Add durable schedule fire ledger.

Revision ID: 098_schedule_fires
Revises: 097_direct_turn_requests
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "098_schedule_fires"
down_revision: str | None = "097_direct_turn_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_fires",
        sa.Column("fire_id", sa.String(), nullable=False),
        sa.Column("schedule_id", sa.String(), nullable=False),
        sa.Column("scheduled_fire_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('claimed', 'dispatched', 'skipped', 'failed')",
            name="ck_schedule_fires_status",
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.schedule_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("fire_id"),
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_fire_at",
            name="uq_schedule_fires_logical_fire",
        ),
    )
    op.create_index(
        "ix_schedule_fires_reconcile",
        "schedule_fires",
        ["status", "updated_at"],
    )
    op.create_index("ix_schedule_fires_task", "schedule_fires", ["task_id"])
    op.create_table(
        "schedule_catchup_state",
        sa.Column("catchup_id", sa.String(), nullable=False),
        sa.Column("cutoff_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("remaining_budget", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_schedule_catchup_state_status",
        ),
        sa.PrimaryKeyConstraint("catchup_id"),
    )
    op.create_table(
        "channel_account_operations",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["channel_accounts.account_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("account_id"),
    )


def downgrade() -> None:
    op.drop_table("channel_account_operations")
    op.drop_table("schedule_catchup_state")
    op.drop_index("ix_schedule_fires_task", table_name="schedule_fires")
    op.drop_index("ix_schedule_fires_reconcile", table_name="schedule_fires")
    op.drop_table("schedule_fires")
