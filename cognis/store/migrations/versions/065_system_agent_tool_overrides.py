"""Add tool and permission overrides for system agents."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "065_system_agent_tool_overrides"
down_revision = "064_agent_runtime_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("system_agent_overrides", sa.Column("tools_override", sa.JSON(), nullable=True))
    op.add_column(
        "system_agent_overrides", sa.Column("permissions_override", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("system_agent_overrides", "permissions_override")
    op.drop_column("system_agent_overrides", "tools_override")
