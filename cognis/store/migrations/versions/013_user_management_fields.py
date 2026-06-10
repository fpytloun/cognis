"""Add user management fields: is_active, updated_at, last_login_at, disabled_at, disabled_by."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013_user_management_fields"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "users",
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("disabled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("disabled_by", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "disabled_by")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "is_active")
