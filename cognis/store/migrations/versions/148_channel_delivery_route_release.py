"""Persist explicit channel-delivery route releases.

Revision ID: 148_channel_delivery_route_release
Revises: 147_session_runtime_overrides
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "148_channel_delivery_route_release"
down_revision = "147_session_runtime_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "route_released_at" not in columns:
            batch_op.add_column(
                sa.Column("route_released_at", sa.TIMESTAMP(timezone=True), nullable=True)
            )
        if "route_release_audit" not in columns:
            batch_op.add_column(sa.Column("route_release_audit", sa.JSON(), nullable=True))


def downgrade() -> None:
    columns = {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "route_release_audit" in columns:
            batch_op.drop_column("route_release_audit")
        if "route_released_at" in columns:
            batch_op.drop_column("route_released_at")
