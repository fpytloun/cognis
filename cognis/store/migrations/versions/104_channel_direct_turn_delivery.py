"""Add fenced direct-turn metadata to channel delivery outbox."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "104_channel_direct_turn_delivery"
down_revision: str | Sequence[str] | None = "103_orm_schema_parity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channel_delivery_outbox", sa.Column("reply_to_id", sa.String()))
    op.add_column("channel_delivery_outbox", sa.Column("direct_turn_request_id", sa.String()))
    op.add_column(
        "channel_delivery_outbox",
        sa.Column("direct_turn_fencing_token", sa.BigInteger()),
    )


def downgrade() -> None:
    op.drop_column("channel_delivery_outbox", "direct_turn_fencing_token")
    op.drop_column("channel_delivery_outbox", "direct_turn_request_id")
    op.drop_column("channel_delivery_outbox", "reply_to_id")
