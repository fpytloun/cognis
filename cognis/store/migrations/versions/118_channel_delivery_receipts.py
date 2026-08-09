"""Persist authoritative channel delivery chunk receipts.

Revision ID: 118_channel_delivery_receipts
Revises: 117_group_context
"""

import sqlalchemy as sa
from alembic import op

revision = "118_channel_delivery_receipts"
down_revision = "117_group_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "delivery_receipts_json" not in columns:
            batch_op.add_column(sa.Column("delivery_receipts_json", sa.JSON(), nullable=True))
        if "first_delivered_at" not in columns:
            batch_op.add_column(
                sa.Column("first_delivered_at", sa.TIMESTAMP(timezone=True), nullable=True)
            )
        if "last_delivered_at" not in columns:
            batch_op.add_column(
                sa.Column("last_delivered_at", sa.TIMESTAMP(timezone=True), nullable=True)
            )
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "channel_delivery_receipts" not in tables:
        op.create_table(
            "channel_delivery_receipts",
            sa.Column(
                "delivery_id",
                sa.String(),
                sa.ForeignKey("channel_delivery_outbox.delivery_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("chunk_index", sa.Integer(), primary_key=True),
            sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("external_message_id", sa.String(), nullable=True),
            sa.Column("attachments_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_channel_delivery_receipts_sent",
            "channel_delivery_receipts",
            ["sent_at", "delivery_id", "chunk_index"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "channel_delivery_receipts" in tables:
        op.drop_index(
            "ix_channel_delivery_receipts_sent",
            table_name="channel_delivery_receipts",
        )
        op.drop_table("channel_delivery_receipts")
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "last_delivered_at" in columns:
            batch_op.drop_column("last_delivered_at")
        if "first_delivered_at" in columns:
            batch_op.drop_column("first_delivered_at")
        if "delivery_receipts_json" in columns:
            batch_op.drop_column("delivery_receipts_json")
