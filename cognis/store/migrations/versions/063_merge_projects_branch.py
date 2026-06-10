"""Merge projects branch into the main migration chain.

Revision ID: 063_merge_projects_branch
Revises: 062_managed_conversation_links, 046_projects_and_revisions
"""

from __future__ import annotations

revision = "063_merge_projects_branch"
down_revision = ("062_managed_conversation_links", "046_projects_and_revisions")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op merge revision."""


def downgrade() -> None:
    """No-op merge revision."""
