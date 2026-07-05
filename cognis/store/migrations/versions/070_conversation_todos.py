"""Add authoritative conversation TODO state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "070_conversation_todos"
down_revision = "069_session_todos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_todos",
        sa.Column("conversation_id", sa.String(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id", "position"),
    )
    op.create_index(
        "ix_conversation_todos_conversation",
        "conversation_todos",
        ["conversation_id"],
    )
    op.execute(
        """
        INSERT INTO conversation_todos (
            conversation_id,
            position,
            content,
            status,
            priority,
            created_at,
            updated_at
        )
        SELECT
            selected.conversation_id,
            st.position,
            st.content,
            st.status,
            st.priority,
            st.created_at,
            st.updated_at
        FROM (
            SELECT
                s.conversation_id AS conversation_id,
                s.session_id AS session_id,
                ROW_NUMBER() OVER (
                    PARTITION BY s.conversation_id
                    ORDER BY
                        CASE WHEN c.active_session_id = s.session_id THEN 0 ELSE 1 END,
                        s.updated_at DESC,
                        s.started_at DESC
                ) AS rn
            FROM sessions s
            JOIN conversations c ON c.conversation_id = s.conversation_id
            WHERE (
                c.active_session_id = s.session_id
                OR (c.active_session_id IS NULL AND s.status IN ('active', 'idle'))
            )
        ) selected
        JOIN session_todos st ON st.session_id = selected.session_id
        WHERE selected.rn = 1
          AND NOT EXISTS (
              SELECT 1
              FROM conversation_todos existing
              WHERE existing.conversation_id = selected.conversation_id
          )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_todos_conversation", table_name="conversation_todos")
    op.drop_table("conversation_todos")
