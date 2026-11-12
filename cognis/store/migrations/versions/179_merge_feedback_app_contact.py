"""Join independently merged work-feedback and app-contact migrations."""

from __future__ import annotations

revision = "179_merge_feedback_app_contact"
down_revision = ("178_feedback_capabilities", "178_app_contact_titles")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both parents already perform their independent schema changes."""


def downgrade() -> None:
    """Restore the two parent heads without changing their schemas."""
