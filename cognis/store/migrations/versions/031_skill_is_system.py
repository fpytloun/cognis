"""Add is_system flag to skills.

Revision ID: 031_skill_is_system
Revises: 030_mcp_http_headers
Create Date: 2026-04-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031_skill_is_system"
down_revision = "030_mcp_http_headers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "skills",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("skills", "is_system")
