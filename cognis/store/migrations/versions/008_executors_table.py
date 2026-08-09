"""Add executors table for tool-to-executor assignment.

Revision ID: 008
Revises: 007
Create Date: 2026-03-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "executors",
        sa.Column("executor_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("executor_type", sa.String(), nullable=False, server_default="in_process"),
        sa.Column("labels", sa.JSON(), nullable=True),
        sa.Column("enabled_tools", sa.JSON(), nullable=True),
        sa.Column("enabled_tool_groups", sa.JSON(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "owner_email",
            sa.String(),
            sa.ForeignKey("users.email"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("executors")
