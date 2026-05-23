"""Add active executor lifecycle metadata.

Revision ID: 053_active_executor_lifecycle
Revises: 052_task_active_executor
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "053_active_executor_lifecycle"
down_revision = "052_task_active_executor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("conversations", "tasks"):
        op.add_column(
            table_name,
            sa.Column("active_executor_assigned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("active_executor_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("active_executor_source", sa.String(), nullable=True),
        )


def downgrade() -> None:
    for table_name in ("tasks", "conversations"):
        op.drop_column(table_name, "active_executor_source")
        op.drop_column(table_name, "active_executor_expires_at")
        op.drop_column(table_name, "active_executor_assigned_at")
