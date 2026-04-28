"""Add preferred task delivery marker to channel accounts.

Revision ID: 047_channel_preferred_delivery
Revises: 046_agent_grantee_overrides
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "047_channel_preferred_delivery"
down_revision = "046_agent_grantee_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_accounts",
        sa.Column(
            "preferred_for_task_delivery",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("channel_accounts", "preferred_for_task_delivery")
