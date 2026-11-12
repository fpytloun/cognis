"""Join usage accounting with document sharing and conversation title retry."""

from __future__ import annotations

revision = "185_merge_usage_heads"
down_revision = ("184_usage_session_day_index", "181_merge_document_sharing")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both branches already apply their independent schema changes."""


def downgrade() -> None:
    """Restore the parent heads without changing their schemas."""
