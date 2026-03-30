"""Add previous_session_id and completion_reason to sessions table.

These fields support session rotation on compaction: previous_session_id
links the session chain after compaction, and completion_reason records
why a session was completed (e.g., "compacted", "user_reset", "max_age").

Revision ID: 009
Revises: 008
Create Date: 2026-03-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("previous_session_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("completion_reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("completion_reason")
        batch_op.drop_column("previous_session_id")
