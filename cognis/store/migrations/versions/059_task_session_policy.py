"""Add task session policy.

Revision ID: 059_task_session_policy
Revises: 058_task_creator_agent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "059_task_session_policy"
down_revision = "058_task_creator_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("session_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("session_policy")
