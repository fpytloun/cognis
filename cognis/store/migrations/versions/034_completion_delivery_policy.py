"""Add explicit completion delivery policy fields.

Revision ID: 034_completion_delivery_policy
Revises: 033_task_working_directory, 033_system_agent_skill_overrides
Create Date: 2026-04-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "034_completion_delivery_policy"
down_revision = ("033_task_working_directory", "033_system_agent_skill_overrides")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("completion_mode_family", sa.String(), nullable=False, server_default="default"),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "allow_silent_completion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("tasks", sa.Column("applied_completion_mode", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("applied_completion_reason", sa.Text(), nullable=True))

    op.add_column(
        "schedules",
        sa.Column("completion_mode_family", sa.String(), nullable=False, server_default="default"),
    )
    op.add_column(
        "schedules",
        sa.Column(
            "allow_silent_completion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.execute(
        sa.text(
            "UPDATE schedules SET completion_mode_family = 'default', allow_silent_completion = suppress_empty"
        )
    )

    with op.batch_alter_table("schedules") as batch_op:
        batch_op.drop_column("suppress_empty")


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch_op:
        batch_op.add_column(
            sa.Column("suppress_empty", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )

    op.execute(sa.text("UPDATE schedules SET suppress_empty = 1 WHERE allow_silent_completion = 1"))

    op.drop_column("schedules", "allow_silent_completion")
    op.drop_column("schedules", "completion_mode_family")

    op.drop_column("tasks", "applied_completion_reason")
    op.drop_column("tasks", "applied_completion_mode")
    op.drop_column("tasks", "allow_silent_completion")
    op.drop_column("tasks", "completion_mode_family")
