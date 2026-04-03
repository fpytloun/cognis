"""Add conversation_id column to step_runs table.

Each workflow step creates its own conversation+session.  Storing the
conversation_id directly on the step_run record allows the UI to open
session logs for any step attempt (including failed retries) without
heuristic title matching.

Revision ID: 012
Revises: 011
Create Date: 2026-03-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("step_runs", sa.Column("conversation_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("step_runs", "conversation_id")
