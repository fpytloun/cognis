"""Add task source-session provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "125_task_source_session"
down_revision: str | Sequence[str] | None = "124_session_activity_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {str(column["name"]) for column in inspector.get_columns("tasks")}
    if "source_session_id" not in columns:
        op.add_column("tasks", sa.Column("source_session_id", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE tasks
        SET source_session_id = (
            SELECT sessions.session_id
            FROM sessions
            WHERE sessions.conversation_id = tasks.source_ref
              AND sessions.started_at <= tasks.created_at
              AND (
                  sessions.completed_at IS NULL
                  OR sessions.completed_at >= tasks.created_at
              )
        )
        WHERE tasks.source_session_id IS NULL
          AND tasks.source_type IN ('chat', 'agent')
          AND (
              SELECT COUNT(*)
              FROM sessions
              WHERE sessions.conversation_id = tasks.source_ref
                AND sessions.started_at <= tasks.created_at
                AND (
                    sessions.completed_at IS NULL
                    OR sessions.completed_at >= tasks.created_at
                )
          ) = 1
        """
    )
    indexes = {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes("tasks")
        if index.get("name")
    }
    if "ix_tasks_owner_source_session" not in indexes:
        op.create_index(
            "ix_tasks_owner_source_session",
            "tasks",
            ["created_by", "source_session_id", "task_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_tasks_owner_source_session", table_name="tasks")
    op.drop_column("tasks", "source_session_id")
