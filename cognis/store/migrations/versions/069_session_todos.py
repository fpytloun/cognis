"""Add authoritative session TODO state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "069_session_todos"
down_revision = "068_task_board_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_todos",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=True),
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
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", "position"),
    )
    op.create_index("ix_session_todos_session", "session_todos", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_session_todos_session", table_name="session_todos")
    op.drop_table("session_todos")
