"""Add runtime profile selection to schedules."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "066_schedule_agent_profiles"
down_revision = "065_system_agent_tool_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedules", sa.Column("agent_profile_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("schedules", "agent_profile_id")
