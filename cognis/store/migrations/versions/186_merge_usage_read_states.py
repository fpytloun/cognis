"""Join usage accounting with conversation read states."""

from __future__ import annotations

revision = "186_merge_usage_read_states"
down_revision = ("185_merge_usage_heads", "182_conversation_read_states")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both branches already apply their independent schema changes."""


def downgrade() -> None:
    """Restore the parent heads without changing their schemas."""
