"""Add per-agent runtime profile selection columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "064_agent_runtime_profiles"
down_revision = "063_merge_projects_branch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("agent_profiles", sa.JSON(), nullable=True))
    op.add_column("agents", sa.Column("default_agent_profile_id", sa.String(), nullable=True))
    op.add_column("conversations", sa.Column("agent_profile_id", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("agent_profile_id", sa.String(), nullable=True))
    op.add_column(
        "managed_conversation_links",
        sa.Column("target_agent_profile_id", sa.String(), nullable=True),
    )
    op.add_column("tasks", sa.Column("agent_profile_id", sa.String(), nullable=True))
    op.add_column("step_runs", sa.Column("agent_profile_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("step_runs", "agent_profile_id")
    op.drop_column("tasks", "agent_profile_id")
    op.drop_column("managed_conversation_links", "target_agent_profile_id")
    op.drop_column("sessions", "agent_profile_id")
    op.drop_column("conversations", "agent_profile_id")
    op.drop_column("agents", "default_agent_profile_id")
    op.drop_column("agents", "agent_profiles")
