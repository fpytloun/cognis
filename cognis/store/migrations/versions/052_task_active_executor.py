"""Add tasks.active_executor_id (Stage 36 task-level executor pin).

Workflow steps each create their own conversation; without a task-level
pin, every step would re-pick a primary executor independently. The task
pin is the durable carrier of the agent's executor choice across all
steps of a single task.

Revision ID: 052_task_active_executor
Revises: 051_conversation_active_executor
Create Date: 2026-05-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "052_task_active_executor"
down_revision = "051_conversation_active_executor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("active_executor_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "active_executor_id")
