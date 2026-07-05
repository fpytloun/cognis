"""Add Chat v2 client transaction ledger."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "071_chat_client_transactions"
down_revision = "7a9390c1ea82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_client_transactions",
        sa.Column("transaction_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("client_txn_id", sa.String(), nullable=False),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["users.email"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transaction_id"),
        sa.UniqueConstraint(
            "conversation_id",
            "principal_id",
            "client_txn_id",
            "operation",
            name="uq_chat_client_transactions_key",
        ),
    )
    op.create_index(
        "ix_chat_client_transactions_conversation",
        "chat_client_transactions",
        ["conversation_id"],
    )
    op.create_index(
        "ix_chat_client_transactions_principal",
        "chat_client_transactions",
        ["principal_id"],
    )
    op.create_index(
        "ix_chat_client_transactions_updated",
        "chat_client_transactions",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_client_transactions_updated",
        table_name="chat_client_transactions",
    )
    op.drop_index(
        "ix_chat_client_transactions_principal",
        table_name="chat_client_transactions",
    )
    op.drop_index(
        "ix_chat_client_transactions_conversation",
        table_name="chat_client_transactions",
    )
    op.drop_table("chat_client_transactions")
