"""add_agent_capabilities

Adds a ``capabilities`` JSON column to the ``agents`` table for per-agent
backend selection (memory_backend, guardrails_backend).

Revision ID: 7a9390c1ea82
Revises: 070_conversation_todos
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7a9390c1ea82"
down_revision = "070_conversation_todos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(
            sa.Column("capabilities", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("capabilities")
