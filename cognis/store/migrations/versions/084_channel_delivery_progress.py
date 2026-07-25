"""Add resumable channel delivery chunk progress.

Revision ID: 084_channel_delivery_progress
Revises: 083_follow_up_leases
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "084_channel_delivery_progress"
down_revision: str | Sequence[str] | None = "083_follow_up_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("channel_delivery_outbox")
    }
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        if "completed_chunk_count" not in columns:
            batch_op.add_column(
                sa.Column(
                    "completed_chunk_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "projected_chunk_count" not in columns:
            batch_op.add_column(sa.Column("projected_chunk_count", sa.Integer(), nullable=True))
        if "projection_digest" not in columns:
            batch_op.add_column(sa.Column("projection_digest", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("channel_delivery_outbox") as batch_op:
        batch_op.drop_column("projection_digest")
        batch_op.drop_column("projected_chunk_count")
        batch_op.drop_column("completed_chunk_count")
