"""Add executor runtime metadata field.

Revision ID: 026_executor_runtime_metadata
Revises: 025_channel_delivery_outbox
Create Date: 2026-04-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026_executor_runtime_metadata"
down_revision = "025_channel_delivery_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("executors", sa.Column("runtime_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("executors", "runtime_metadata")
