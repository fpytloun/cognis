"""Add last_used_at timestamp to api_keys."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_api_key_last_used_at"
down_revision = "003_tasks_workflows_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "last_used_at")
