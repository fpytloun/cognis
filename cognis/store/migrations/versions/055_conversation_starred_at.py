"""Add conversation starred timestamp."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "055_conversation_starred_at"
down_revision = "054_session_result_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("starred_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "starred_at")
