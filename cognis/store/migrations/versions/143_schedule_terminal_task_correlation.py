"""Correlate schedules with their canonically projected terminal task.

Revision ID: 143_schedule_terminal_task_correlation
Revises: 142_schedule_action_issue_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "143_schedule_terminal_task_correlation"
down_revision = "142_schedule_action_issue_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("schedules")}
    if "last_terminal_task_id" not in columns:
        op.add_column("schedules", sa.Column("last_terminal_task_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedules", "last_terminal_task_id")
