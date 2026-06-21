"""Add per-user UI state table."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "067_user_ui_state"
down_revision = "066_schedule_agent_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_ui_state",
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_email", "key"),
    )
    op.create_index("ix_user_ui_state_user_email", "user_ui_state", ["user_email"])


def downgrade() -> None:
    op.drop_index("ix_user_ui_state_user_email", table_name="user_ui_state")
    op.drop_table("user_ui_state")
