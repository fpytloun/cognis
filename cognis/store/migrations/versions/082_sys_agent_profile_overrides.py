"""Persist per-user system-agent runtime profile overrides.

Revision ID: 082_sys_agent_profile_overrides
Revises: 081_mcp_oauth_refresh_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "082_sys_agent_profile_overrides"
down_revision: str | Sequence[str] | None = "081_mcp_oauth_refresh_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("system_agent_overrides")
    }
    with op.batch_alter_table("system_agent_overrides") as batch_op:
        if "agent_profiles_override" not in columns:
            batch_op.add_column(sa.Column("agent_profiles_override", sa.JSON(), nullable=True))
        if "default_agent_profile_id_override" not in columns:
            batch_op.add_column(
                sa.Column("default_agent_profile_id_override", sa.String(), nullable=True)
            )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("system_agent_overrides")
    }
    with op.batch_alter_table("system_agent_overrides") as batch_op:
        if "default_agent_profile_id_override" in columns:
            batch_op.drop_column("default_agent_profile_id_override")
        if "agent_profiles_override" in columns:
            batch_op.drop_column("agent_profiles_override")
