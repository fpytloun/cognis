"""Add expected_output column to tasks table.

Allows users and agents to specify what output format or content a task
should produce.  Used by the workflow engine to include expected output
criteria in step prompts and by the step evaluator for completion checks.

Revision ID: 010
Revises: 009
Create Date: 2026-03-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("expected_output", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "expected_output")
