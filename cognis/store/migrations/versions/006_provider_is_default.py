"""Add is_default to llm_providers.

Revision ID: 006
Revises: 005_agent_sync_metadata
Create Date: 2026-03-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005_agent_sync_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_providers",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("llm_providers", "is_default")
