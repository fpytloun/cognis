"""Add linked_tool_ids to skills.

Revision ID: 042_skill_linked_tool_ids
Revises: 041_schedule_skill_sources
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "042_skill_linked_tool_ids"
down_revision = "041_schedule_skill_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("skills") as batch:
        batch.add_column(sa.Column("linked_tool_ids", sa.JSON(), nullable=True))
    with op.batch_alter_table("skill_versions") as batch:
        batch.add_column(sa.Column("linked_tool_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("skill_versions") as batch:
        batch.drop_column("linked_tool_ids")
    with op.batch_alter_table("skills") as batch:
        batch.drop_column("linked_tool_ids")
