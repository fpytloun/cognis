"""Add task board pagination indexes."""

from __future__ import annotations

from alembic import op

revision = "068_task_board_indexes"
down_revision = "067_user_ui_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_tasks_owner_updated", "tasks", ["created_by", "updated_at", "task_id"])
    op.create_index(
        "ix_tasks_owner_status_updated",
        "tasks",
        ["created_by", "status", "updated_at", "task_id"],
    )
    op.create_index(
        "ix_tasks_owner_agent_updated",
        "tasks",
        ["created_by", "agent_id", "updated_at", "task_id"],
    )
    op.create_index(
        "ix_tasks_owner_project_updated",
        "tasks",
        ["created_by", "project_id", "updated_at", "task_id"],
    )
    op.create_index(
        "ix_tasks_owner_workflow_updated",
        "tasks",
        ["created_by", "workflow_id", "updated_at", "task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_owner_workflow_updated", table_name="tasks")
    op.drop_index("ix_tasks_owner_project_updated", table_name="tasks")
    op.drop_index("ix_tasks_owner_agent_updated", table_name="tasks")
    op.drop_index("ix_tasks_owner_status_updated", table_name="tasks")
    op.drop_index("ix_tasks_owner_updated", table_name="tasks")
