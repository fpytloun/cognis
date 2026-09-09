"""Add notification attention concurrency fields.

Revision ID: 145_notification_attention_state
Revises: 144_work_v8_projection_repair
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "145_notification_attention_state"
down_revision = "144_work_v8_projection_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {str(column["name"]) for column in inspector.get_columns("notifications")}
    if "revision" not in columns:
        op.add_column(
            "notifications",
            sa.Column("revision", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        )
    if "expires_at" not in columns:
        op.add_column(
            "notifications",
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
    indexes = {
        str(index["name"]) for index in inspector.get_indexes("notifications") if index.get("name")
    }
    if "ix_notifications_user_task_status" not in indexes:
        op.create_index(
            "ix_notifications_user_task_status",
            "notifications",
            ["user_email", "task_id", "status"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {
        str(index["name"]) for index in inspector.get_indexes("notifications") if index.get("name")
    }
    if "ix_notifications_user_task_status" in indexes:
        op.drop_index("ix_notifications_user_task_status", table_name="notifications")
    columns = {str(column["name"]) for column in inspector.get_columns("notifications")}
    if "expires_at" in columns:
        op.drop_column("notifications", "expires_at")
    if "revision" in columns:
        op.drop_column("notifications", "revision")
