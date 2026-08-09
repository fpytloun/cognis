"""Add skills table for DB-managed skill definitions.

Revision ID: 007
Revises: 006
Create Date: 2026-03-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("skill_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=True),
        sa.Column("prompt_templates", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("auto_load", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(), nullable=False, server_default="db"),
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
    op.drop_table("skills")
