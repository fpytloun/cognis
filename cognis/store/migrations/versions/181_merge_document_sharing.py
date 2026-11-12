"""Join document sharing and conversation title retry migrations."""

from __future__ import annotations

revision = "181_merge_document_sharing"
down_revision = ("180_document_sharing", "180_conversation_title_retry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both parents already perform their independent schema changes."""


def downgrade() -> None:
    """Restore the two parent heads without changing their schemas."""
