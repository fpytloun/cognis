"""Add durable native refresh sessions.

Revision ID: 135_native_sessions
Revises: 134_work_activity_list_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "135_native_sessions"
down_revision = "134_work_activity_list_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("native_sessions"):
        return
    op.create_table(
        "native_sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("family_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("token_hash", name="uq_native_sessions_token_hash"),
    )
    op.create_index("ix_native_sessions_family_id", "native_sessions", ["family_id"])
    op.create_index("ix_native_sessions_user_email", "native_sessions", ["user_email"])
    op.create_index("ix_native_sessions_expires_at", "native_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_native_sessions_expires_at", table_name="native_sessions")
    op.drop_index("ix_native_sessions_user_email", table_name="native_sessions")
    op.drop_index("ix_native_sessions_family_id", table_name="native_sessions")
    op.drop_table("native_sessions")
