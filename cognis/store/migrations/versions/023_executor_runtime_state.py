"""Add executor runtime state tracking."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "023_executor_runtime_state"
down_revision = "022_mcp_servers_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executors",
        sa.Column("desired_config_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "executors",
        sa.Column("applied_config_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("executors", sa.Column("observed_tools", sa.JSON(), nullable=True))
    op.add_column(
        "executors",
        sa.Column("last_observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "executors",
        sa.Column("runtime_state", sa.String(), nullable=False, server_default="offline"),
    )


def downgrade() -> None:
    op.drop_column("executors", "runtime_state")
    op.drop_column("executors", "last_observed_at")
    op.drop_column("executors", "observed_tools")
    op.drop_column("executors", "applied_config_version")
    op.drop_column("executors", "desired_config_version")
