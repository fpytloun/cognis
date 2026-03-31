"""Rename root_session_id to active_session_id on conversations.

The active_session_id always points to the current session for a
conversation, updated on every session creation and compaction.
The old root_session_id became stale after compaction, causing result
delivery and event recording to target completed sessions.

Revision ID: 011
Revises: 010
Create Date: 2026-03-31
"""

from __future__ import annotations

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("conversations", "root_session_id", new_column_name="active_session_id")


def downgrade() -> None:
    op.alter_column("conversations", "active_session_id", new_column_name="root_session_id")
