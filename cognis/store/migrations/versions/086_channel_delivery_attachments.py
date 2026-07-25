"""Persist retryable channel delivery attachments.

Revision ID: 086_channel_delivery_attachments
Revises: 085_channel_delivery_inflight
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "086_channel_delivery_attachments"
down_revision: str | Sequence[str] | None = "085_channel_delivery_inflight"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "attachments_json" not in columns:
            batch_op.add_column(sa.Column("attachments_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        batch_op.drop_column("attachments_json")
