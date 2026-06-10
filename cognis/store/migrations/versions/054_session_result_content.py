"""Add durable session result content."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "054_session_result_content"
down_revision = "053_active_executor_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "result_content" not in columns:
        op.add_column("sessions", sa.Column("result_content", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("sessions")}
    if "result_content" in columns:
        op.drop_column("sessions", "result_content")
