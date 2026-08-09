"""Add the durable task control-conversation link."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "109_task_control_conversation"
down_revision: str | Sequence[str] | None = "108_kb_active_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {str(column["name"]) for column in inspector.get_columns("tasks")}
    if "control_conversation_id" not in columns:
        op.add_column(
            "tasks",
            sa.Column("control_conversation_id", sa.String(), nullable=True),
        )
    if "control_conversation_claimed_at" not in columns:
        op.add_column(
            "tasks",
            sa.Column(
                "control_conversation_claimed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    indexes = {str(index["name"]) for index in inspector.get_indexes("tasks") if index.get("name")}
    if "ux_tasks_control_conversation_id" not in indexes:
        op.create_index(
            "ux_tasks_control_conversation_id",
            "tasks",
            ["control_conversation_id"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("ux_tasks_control_conversation_id", table_name="tasks")
    op.drop_column("tasks", "control_conversation_claimed_at")
    op.drop_column("tasks", "control_conversation_id")
