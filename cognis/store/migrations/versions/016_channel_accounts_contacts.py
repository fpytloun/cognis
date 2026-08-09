"""Add channel_accounts and channel_contacts tables.

Channel accounts store configurations for external messaging platform
connections (Signal, WhatsApp, Telegram, etc.).  Channel contacts map
external platform sender IDs to Cognis user emails.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_channel_accounts_contacts"
down_revision = "015_conversation_last_read_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_accounts",
        sa.Column("account_id", sa.String, primary_key=True),
        sa.Column("channel_type", sa.String, nullable=False),
        sa.Column("display_name", sa.String, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "agent_id",
            sa.String,
            sa.ForeignKey("agents.agent_id"),
            nullable=False,
        ),
        sa.Column(
            "user_email",
            sa.String,
            sa.ForeignKey("users.email"),
            nullable=False,
        ),
        sa.Column("config", sa.JSON, nullable=True),
        sa.Column("credential_refs", sa.JSON, nullable=True),
        sa.Column("default_conversation_id", sa.String, nullable=True),
        sa.Column("allow_new_conversations", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("allowed_senders", sa.JSON, nullable=True),
        sa.Column("dm_policy", sa.String, nullable=False, server_default="open"),
        sa.Column("group_policy", sa.String, nullable=False, server_default="mention"),
        sa.Column("webhook_secret", sa.String, nullable=True),
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
    op.create_index("ix_channel_accounts_type", "channel_accounts", ["channel_type"])
    op.create_index("ix_channel_accounts_user", "channel_accounts", ["user_email"])

    op.create_table(
        "channel_contacts",
        sa.Column("contact_id", sa.String, primary_key=True),
        sa.Column("channel_type", sa.String, nullable=False),
        sa.Column("sender_id", sa.String, nullable=False),
        sa.Column(
            "user_email",
            sa.String,
            sa.ForeignKey("users.email"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String, nullable=True),
        sa.Column("verified", sa.Boolean, nullable=False, server_default=sa.false()),
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
        sa.UniqueConstraint("channel_type", "sender_id", name="uq_channel_contact"),
    )
    op.create_index(
        "ix_channel_contacts_lookup",
        "channel_contacts",
        ["channel_type", "sender_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_channel_contacts_lookup", table_name="channel_contacts")
    op.drop_table("channel_contacts")
    op.drop_index("ix_channel_accounts_user", table_name="channel_accounts")
    op.drop_index("ix_channel_accounts_type", table_name="channel_accounts")
    op.drop_table("channel_accounts")
