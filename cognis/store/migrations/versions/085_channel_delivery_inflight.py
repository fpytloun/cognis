"""Track in-flight channel delivery chunks.

Revision ID: 085_channel_delivery_inflight
Revises: 084_channel_delivery_progress
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "085_channel_delivery_inflight"
down_revision: str | Sequence[str] | None = "084_channel_delivery_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "inflight_chunk_index" not in columns:
            batch_op.add_column(sa.Column("inflight_chunk_index", sa.Integer(), nullable=True))
        if "inflight_idempotent" not in columns:
            batch_op.add_column(sa.Column("inflight_idempotent", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        batch_op.drop_column("inflight_idempotent")
        batch_op.drop_column("inflight_chunk_index")
