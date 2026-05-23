"""Add durable session result content."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "054_session_result_content"
down_revision = "053_active_executor_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("result_content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "result_content")
