"""Add managed-channel lifecycle fencing metadata.

Revision ID: 113_managed_channel_lifecycle
Revises: 112_channel_observed_targets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "113_managed_channel_lifecycle"
down_revision: str | Sequence[str] | None = "112_channel_observed_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    binding_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("managed_channel_bindings")
    }
    with op.batch_alter_table("managed_channel_bindings") as batch:
        if "channel_type" not in binding_columns:
            batch.add_column(
                sa.Column("channel_type", sa.String(), server_default="", nullable=False)
            )
    target_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("channel_observed_targets")
    }
    with op.batch_alter_table("channel_observed_targets") as batch:
        if "thread_id" not in target_columns:
            batch.add_column(sa.Column("thread_id", sa.String(), nullable=True))
        if "sender_id" not in target_columns:
            batch.add_column(sa.Column("sender_id", sa.String(), nullable=True))
    outbox_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("channel_delivery_outbox")
    }
    outbox_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch:
        if "managed_binding_id" not in outbox_columns:
            batch.add_column(sa.Column("managed_binding_id", sa.String(), nullable=True))
        if "managed_binding_version" not in outbox_columns:
            batch.add_column(sa.Column("managed_binding_version", sa.BigInteger(), nullable=True))
        if "managed_owner_epoch" not in outbox_columns:
            batch.add_column(sa.Column("managed_owner_epoch", sa.BigInteger(), nullable=True))
        if "ix_channel_delivery_managed_fence" not in outbox_indexes:
            batch.create_index(
                "ix_channel_delivery_managed_fence",
                ["managed_binding_id", "managed_binding_version", "managed_owner_epoch"],
            )


def downgrade() -> None:
    with op.batch_alter_table("channel_delivery_outbox") as batch:
        batch.drop_index("ix_channel_delivery_managed_fence")
        batch.drop_column("managed_owner_epoch")
        batch.drop_column("managed_binding_version")
        batch.drop_column("managed_binding_id")
    with op.batch_alter_table("channel_observed_targets") as batch:
        batch.drop_column("sender_id")
        batch.drop_column("thread_id")
    with op.batch_alter_table("managed_channel_bindings") as batch:
        batch.drop_column("channel_type")
