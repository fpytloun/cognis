"""Add conversations.active_executor_id (Stage 36 multi-executor agents).

Revision ID: 051_conversation_active_executor
Revises: 050_executor_token_version
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "051_conversation_active_executor"
down_revision = "050_executor_token_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("active_executor_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "active_executor_id")
