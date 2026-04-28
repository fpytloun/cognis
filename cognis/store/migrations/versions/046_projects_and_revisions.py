"""Add projects and revision foundation.

Revision ID: 046_projects_and_revisions
Revises: 045_push_subscriptions
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "046_projects_and_revisions"
down_revision = "045_push_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("owner_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("default_workflow_id", sa.String(), nullable=True),
        sa.Column("avatar_image_id", sa.String(), nullable=True),
        sa.Column("avatar_url", sa.String(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_projects_owner_email", "projects", ["owner_email"])

    op.create_table(
        "project_sources",
        sa.Column("source_id", sa.String(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("default_branch", sa.String(), nullable=True),
        sa.Column("credential_ref", sa.String(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_project_sources_project_id", "project_sources", ["project_id"])
    op.create_index("ix_project_sources_local_path", "project_sources", ["local_path"])

    op.create_table(
        "project_workflows",
        sa.Column(
            "project_id",
            sa.String(),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("workflow_id", sa.String(), primary_key=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "project_grants",
        sa.Column("grant_id", sa.String(), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grantee_type", sa.String(), nullable=False, server_default="user"),
        sa.Column("grantee_user_email", sa.String(), sa.ForeignKey("users.email"), nullable=True),
        sa.Column("grantee_group_id", sa.String(), nullable=True),
        sa.Column("permission", sa.String(), nullable=False, server_default="use"),
        sa.Column("granted_by", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column(
            "granted_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_project_grants_grantee_user", "project_grants", ["grantee_user_email"])
    op.create_index("ix_project_grants_project", "project_grants", ["project_id"])
    op.create_index(
        "uq_project_grants_active_user",
        "project_grants",
        ["project_id", "grantee_user_email"],
        unique=True,
        sqlite_where=sa.text("grantee_type = 'user' AND revoked_at IS NULL"),
        postgresql_where=sa.text("grantee_type = 'user' AND revoked_at IS NULL"),
    )

    op.add_column("tasks", sa.Column("project_id", sa.String(), nullable=True))
    op.add_column(
        "tasks", sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.add_column("schedules", sa.Column("project_id", sa.String(), nullable=True))
    op.create_index("ix_schedules_project_id", "schedules", ["project_id"])
    op.add_column("conversations", sa.Column("project_id", sa.String(), nullable=True))
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])
    op.add_column(
        "step_runs", sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("step_runs", sa.Column("superseded_by_step_run_id", sa.String(), nullable=True))
    op.create_index("ix_step_runs_superseded_by", "step_runs", ["superseded_by_step_run_id"])
    op.add_column(
        "deliverables",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "task_comments",
        sa.Column("comment_id", sa.String(), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False, server_default="record_only"),
        sa.Column("noop", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("target_step", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_task_comments_task", "task_comments", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_comments_task", table_name="task_comments")
    op.drop_table("task_comments")
    op.drop_column("deliverables", "attempt_number")
    op.drop_index("ix_step_runs_superseded_by", table_name="step_runs")
    op.drop_column("step_runs", "superseded_by_step_run_id")
    op.drop_column("step_runs", "attempt_number")
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    op.drop_column("conversations", "project_id")
    op.drop_index("ix_schedules_project_id", table_name="schedules")
    op.drop_column("schedules", "project_id")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_column("tasks", "attempt_number")
    op.drop_column("tasks", "project_id")
    op.drop_index("uq_project_grants_active_user", table_name="project_grants")
    op.drop_index("ix_project_grants_project", table_name="project_grants")
    op.drop_index("ix_project_grants_grantee_user", table_name="project_grants")
    op.drop_table("project_grants")
    op.drop_table("project_workflows")
    op.drop_index("ix_project_sources_local_path", table_name="project_sources")
    op.drop_index("ix_project_sources_project_id", table_name="project_sources")
    op.drop_table("project_sources")
    op.drop_index("ix_projects_owner_email", table_name="projects")
    op.drop_table("projects")
