"""Add conversation title generation cooldown metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "180_conversation_title_retry"
down_revision = "179_merge_feedback_app_contact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add retry timing without backfilling or changing existing titles."""
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversations")}
    if "title_generation_retry_at" in columns:
        return
    op.add_column(
        "conversations",
        sa.Column("title_generation_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove retry metadata while preserving visible titles."""
    op.drop_column("conversations", "title_generation_retry_at")
