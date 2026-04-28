"""Add per-grantee agent override storage.

Revision ID: 046_agent_grantee_overrides
Revises: 045_push_subscriptions
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "046_agent_grantee_overrides"
down_revision = "045_push_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_grants", sa.Column("grantee_overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_grants", "grantee_overrides")
