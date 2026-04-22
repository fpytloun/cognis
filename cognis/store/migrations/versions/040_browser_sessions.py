"""Add browser sessions table.

Revision ID: 040_browser_sessions
Revises: 039_tool_classification_overrides
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "040_browser_sessions"
down_revision = "039_tool_classification_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column(
            "user_email",
            sa.String(),
            sa.ForeignKey("users.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "last_used_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
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
        sa.UniqueConstraint("token_hash", name="uq_browser_sessions_token_hash"),
    )
    op.create_index(
        "ix_browser_sessions_user_email", "browser_sessions", ["user_email"], unique=False
    )
    op.create_index(
        "ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_expires_at", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_user_email", table_name="browser_sessions")
    op.drop_table("browser_sessions")
