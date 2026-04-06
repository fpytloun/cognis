"""Add durable outbox for channel follow-up delivery.

Revision ID: 025_channel_delivery_outbox
Revises: 024_skill_versions
Create Date: 2026-04-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025_channel_delivery_outbox"
down_revision = "024_skill_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_delivery_outbox",
        sa.Column("delivery_id", sa.String(), primary_key=True),
        sa.Column("user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("channel_type", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("thread_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("fallback_text", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        "ix_channel_delivery_status_due",
        "channel_delivery_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_channel_delivery_conversation_created",
        "channel_delivery_outbox",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "ix_channel_delivery_source",
        "channel_delivery_outbox",
        ["source_type", "source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_channel_delivery_source", table_name="channel_delivery_outbox")
    op.drop_index("ix_channel_delivery_conversation_created", table_name="channel_delivery_outbox")
    op.drop_index("ix_channel_delivery_status_due", table_name="channel_delivery_outbox")
    op.drop_table("channel_delivery_outbox")
