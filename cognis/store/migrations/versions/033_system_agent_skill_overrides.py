"""Add skill overrides for system agents.

Revision ID: 033_system_agent_skill_overrides
Revises: 032_system_asset_overrides
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033_system_agent_skill_overrides"
down_revision = "032_system_asset_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("system_agent_overrides", sa.Column("skills_override", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("system_agent_overrides", "skills_override")
