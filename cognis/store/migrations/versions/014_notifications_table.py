"""Add notifications table for unified escalation/gate/step-question tracking."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014_notifications_table"
down_revision = "013_user_management_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String, primary_key=True),
        sa.Column("notification_type", sa.String, nullable=False),
        sa.Column("user_email", sa.String, sa.ForeignKey("users.email"), nullable=False),
        sa.Column("conversation_id", sa.String, nullable=False),
        sa.Column("task_id", sa.String, nullable=True),
        sa.Column("step_name", sa.String, nullable=True),
        sa.Column("step_run_id", sa.String, nullable=True),
        sa.Column("session_id", sa.String, nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="pending"),
        sa.Column("resolution", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_notifications_user_status",
        "notifications",
        ["user_email", "status"],
    )
    op.create_index(
        "ix_notifications_conv_status",
        "notifications",
        ["conversation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_conv_status", table_name="notifications")
    op.drop_index("ix_notifications_user_status", table_name="notifications")
    op.drop_table("notifications")
