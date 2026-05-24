"""Track task author agent.

Revision ID: 058_task_creator_agent
Revises: 057_knowledgebases
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "058_task_creator_agent"
down_revision = "057_knowledgebases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("created_by_agent_id", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("created_by_agent_id")
