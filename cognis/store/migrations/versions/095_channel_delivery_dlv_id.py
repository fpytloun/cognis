"""Persist deliverable references for retryable channel deliveries.

Revision ID: 095_channel_delivery_dlv_id
Revises: 094_artifact_tool_source_id
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "095_channel_delivery_dlv_id"
down_revision: str | None = "094_artifact_tool_source_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_delivery_outbox",
        sa.Column("deliverable_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_delivery_outbox", "deliverable_id")
