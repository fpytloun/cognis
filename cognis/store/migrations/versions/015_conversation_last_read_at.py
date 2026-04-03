"""Add last_read_at column to conversations for unread tracking."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015_conversation_last_read_at"
down_revision = "014_notifications_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("last_read_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "last_read_at")
