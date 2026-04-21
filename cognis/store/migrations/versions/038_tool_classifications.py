"""Add durable tool classification state.

Revision ID: 038_tool_classifications
Revises: 037_workflow_composition_lifecycle
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "038_tool_classifications"
down_revision = "037_workflow_composition_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_classifications",
        sa.Column("classification_id", sa.String(), primary_key=True),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column(
            "owner_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tool_id", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("tool_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("classification_source", sa.String(), nullable=True),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("scope_key", "tool_id", name="uq_tool_classifications_scope_tool"),
    )
    op.create_index(
        "ix_tool_classifications_status_next_retry",
        "tool_classifications",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_classifications_status_next_retry", table_name="tool_classifications")
    op.drop_table("tool_classifications")
