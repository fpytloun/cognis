"""Add session lifecycle timestamps."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_session_lifecycle_fields"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("idle_since", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.execute("UPDATE sessions SET updated_at = COALESCE(updated_at, started_at)")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("idle_since")
