"""Add scheduled-task failure behavior options.

Revision ID: 138_schedule_task_failure_options
Revises: 137_work_live_projection
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "138_schedule_task_failure_options"
down_revision = "137_work_live_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("schedules")}
    if {"retry_failed_tasks", "fail_paused_task_on_next_fire"} <= columns:
        return
    with op.batch_alter_table("schedules") as batch_op:
        if "retry_failed_tasks" not in columns:
            batch_op.add_column(
                sa.Column(
                    "retry_failed_tasks",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "fail_paused_task_on_next_fire" not in columns:
            batch_op.add_column(
                sa.Column(
                    "fail_paused_task_on_next_fire",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        batch_op.drop_column("fail_paused_task_on_next_fire")
        batch_op.drop_column("retry_failed_tasks")
