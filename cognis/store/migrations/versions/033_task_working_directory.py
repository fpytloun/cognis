"""Add working directory fields to tasks and step runs.

Revision ID: 033_task_working_directory
Revises: 032_system_asset_overrides
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033_task_working_directory"
down_revision = "032_system_asset_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("workspace_root", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("working_directory", sa.Text(), nullable=True))
    op.add_column("step_runs", sa.Column("workspace_root", sa.Text(), nullable=True))
    op.add_column("step_runs", sa.Column("working_directory", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("step_runs", "working_directory")
    op.drop_column("step_runs", "workspace_root")
    op.drop_column("tasks", "working_directory")
    op.drop_column("tasks", "workspace_root")
