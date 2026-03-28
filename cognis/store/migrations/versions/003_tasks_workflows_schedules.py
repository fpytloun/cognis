"""Add tasks, workflows, step_runs, schedules, task_dependencies tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003_tasks_workflows_schedules"
down_revision = "002_session_lifecycle_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("workflow_id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("definition", sa.JSON, nullable=False),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("owner_email", sa.String, sa.ForeignKey("users.email"), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="draft"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.String, sa.ForeignKey("users.email"), nullable=False),
        sa.Column("agent_id", sa.String, sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("source_type", sa.String, nullable=False, server_default="api"),
        sa.Column("source_ref", sa.String, nullable=True),
        sa.Column("delivery_mode", sa.String, nullable=False, server_default="same_conversation"),
        sa.Column("delivery_target", sa.String, nullable=True),
        sa.Column("workflow_id", sa.String, nullable=True),
        sa.Column("workflow_state", sa.JSON, nullable=True),
        sa.Column("queue_name", sa.String, nullable=False, server_default="default"),
        sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("result_data", sa.JSON, nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_tasks_status_priority", "tasks", ["status", "priority"])
    op.create_index("ix_tasks_agent_id", "tasks", ["agent_id"])
    op.create_index("ix_tasks_created_by", "tasks", ["created_by"])

    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.task_id"), primary_key=True),
        sa.Column("depends_on", sa.String, sa.ForeignKey("tasks.task_id"), primary_key=True),
        sa.Column("required", sa.Boolean, nullable=False, server_default=sa.text("1")),
    )

    op.create_table(
        "step_runs",
        sa.Column("step_run_id", sa.String, primary_key=True),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("step_name", sa.String, nullable=False),
        sa.Column("step_type", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("agent_id", sa.String, nullable=False),
        sa.Column("session_id", sa.String, nullable=True),
        sa.Column("intaris_session_id", sa.String, nullable=True),
        sa.Column("output", sa.JSON, nullable=True),
        sa.Column("evaluation", sa.JSON, nullable=True),
        sa.Column("todos", sa.JSON, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_step_runs_task_id", "step_runs", ["task_id"])

    op.create_table(
        "schedules",
        sa.Column("schedule_id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("cron_expr", sa.String, nullable=False),
        sa.Column("agent_id", sa.String, sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("workflow_id", sa.String, nullable=True),
        sa.Column("task_template", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("last_fired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.String, sa.ForeignKey("users.email"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("schedules")
    op.drop_table("step_runs")
    op.drop_table("task_dependencies")
    op.drop_table("tasks")
    op.drop_table("workflows")
