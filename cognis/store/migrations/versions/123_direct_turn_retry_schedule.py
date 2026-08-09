"""Persist the next eligible attempt for durable direct turns."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "123_direct_turn_retry_schedule"
down_revision: str | Sequence[str] | None = "122_durable_work_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "direct_turn_requests",
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_direct_turn_requests_status_due",
        "direct_turn_requests",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_direct_turn_requests_status_due", table_name="direct_turn_requests")
    op.drop_column("direct_turn_requests", "next_attempt_at")
