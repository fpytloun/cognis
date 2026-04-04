"""Add global MCP servers table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022_mcp_servers_table"
down_revision = "021_artifact_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("server_id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("transport", sa.String, nullable=False, server_default="stdio"),
        sa.Column("command", sa.Text, nullable=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("args", sa.JSON, nullable=True),
        sa.Column("env", sa.JSON, nullable=True),
        sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="30"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "owner_email",
            sa.String,
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String, nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "owner_email", name="uq_mcp_server_name_owner"),
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
