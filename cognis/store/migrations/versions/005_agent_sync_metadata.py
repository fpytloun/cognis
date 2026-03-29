"""Add sync_metadata JSON column to agents."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_agent_sync_metadata"
down_revision = "004_api_key_last_used_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "sync_metadata",
            sa.JSON(),
            nullable=True,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "sync_metadata")
