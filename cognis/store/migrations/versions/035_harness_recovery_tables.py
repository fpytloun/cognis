"""Add durable remember queue and follow-up dedupe tables.

Revision ID: 035_harness_recovery_tables
Revises: 034_completion_delivery_policy
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "035_harness_recovery_tables"
down_revision = "034_completion_delivery_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remember_queue",
        sa.Column("item_id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
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
        "ix_remember_queue_status_due",
        "remember_queue",
        ["status", "next_retry_at"],
    )
    op.create_index("ix_remember_queue_session", "remember_queue", ["session_id"])

    op.create_table(
        "follow_up_dedupe",
        sa.Column("dedupe_key", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("follow_up_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
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
        sa.UniqueConstraint(
            "conversation_id",
            "follow_up_id",
            name="uq_follow_up_dedupe_pair",
        ),
    )
    op.create_index("ix_follow_up_dedupe_expires", "follow_up_dedupe", ["expires_at"])
    op.create_index(
        "ix_follow_up_dedupe_conversation",
        "follow_up_dedupe",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_follow_up_dedupe_conversation", table_name="follow_up_dedupe")
    op.drop_index("ix_follow_up_dedupe_expires", table_name="follow_up_dedupe")
    op.drop_table("follow_up_dedupe")

    op.drop_index("ix_remember_queue_session", table_name="remember_queue")
    op.drop_index("ix_remember_queue_status_due", table_name="remember_queue")
    op.drop_table("remember_queue")
