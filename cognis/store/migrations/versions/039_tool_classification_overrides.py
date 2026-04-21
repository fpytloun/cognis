"""Add tool classification overrides.

Revision ID: 039_tool_classification_overrides
Revises: 038_tool_classifications
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "039_tool_classification_overrides"
down_revision = "038_tool_classifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_classification_overrides",
        sa.Column("override_id", sa.String(), primary_key=True),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column(
            "owner_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("profile_group", sa.String(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "scope_key",
            "tool_id",
            name="uq_tool_classification_overrides_scope_tool",
        ),
    )


def downgrade() -> None:
    op.drop_table("tool_classification_overrides")
