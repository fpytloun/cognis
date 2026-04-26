"""Add browser Web Push subscriptions.

Revision ID: 045_push_subscriptions
Revises: 044_step_run_runtime_info
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "045_push_subscriptions"
down_revision = "044_step_run_runtime_info"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("subscription_id", sa.String(), primary_key=True),
        sa.Column(
            "user_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("platform", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_push_subscriptions_user_enabled",
        "push_subscriptions",
        ["user_email", "enabled"],
    )


def downgrade() -> None:
    op.drop_index("ix_push_subscriptions_user_enabled", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
