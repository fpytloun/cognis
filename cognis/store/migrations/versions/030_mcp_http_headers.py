"""Add MCP HTTP headers column."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030_mcp_http_headers"
down_revision = "029_conversation_title_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("headers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_servers", "headers")
