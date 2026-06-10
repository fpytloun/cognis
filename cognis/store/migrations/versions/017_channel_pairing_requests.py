"""Add channel_pairing_requests table for secure remote verification."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017_channel_pairing_requests"
down_revision = "016_channel_accounts_contacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_pairing_requests",
        sa.Column("request_id", sa.String, primary_key=True),
        sa.Column("owner_email", sa.String, sa.ForeignKey("users.email"), nullable=False),
        sa.Column(
            "account_id",
            sa.String,
            sa.ForeignKey("channel_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.String, nullable=False),
        sa.Column("sender_id", sa.String, nullable=False),
        sa.Column("sender_name", sa.String, nullable=True),
        sa.Column("chat_id", sa.String, nullable=False),
        sa.Column("chat_name", sa.String, nullable=True),
        sa.Column("code", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_channel_pairing_code"),
    )
    op.create_index(
        "ix_channel_pairing_owner_status",
        "channel_pairing_requests",
        ["owner_email", "status"],
    )
    op.create_index(
        "ix_channel_pairing_sender_status",
        "channel_pairing_requests",
        ["channel_type", "sender_id", "status"],
    )

    with op.batch_alter_table("channel_accounts") as batch_op:
        batch_op.alter_column(
            "dm_policy",
            existing_type=sa.String(),
            server_default="pairing",
        )
        batch_op.alter_column(
            "group_policy",
            existing_type=sa.String(),
            server_default="pairing",
        )


def downgrade() -> None:
    with op.batch_alter_table("channel_accounts") as batch_op:
        batch_op.alter_column(
            "group_policy",
            existing_type=sa.String(),
            server_default="mention",
        )
        batch_op.alter_column(
            "dm_policy",
            existing_type=sa.String(),
            server_default="open",
        )
    op.drop_index("ix_channel_pairing_sender_status", table_name="channel_pairing_requests")
    op.drop_index("ix_channel_pairing_owner_status", table_name="channel_pairing_requests")
    op.drop_table("channel_pairing_requests")
