"""Add adapter_location and executor_id to channel_accounts.

Allows channel adapters to run on a connected executor instead of the
controller, enabling user-local services like signal-cli.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "018_channel_adapter_location"
down_revision = "017_channel_pairing_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_accounts",
        sa.Column("adapter_location", sa.String, nullable=False, server_default="controller"),
    )
    op.add_column(
        "channel_accounts",
        sa.Column("executor_id", sa.String, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("channel_accounts", "executor_id")
    op.drop_column("channel_accounts", "adapter_location")
