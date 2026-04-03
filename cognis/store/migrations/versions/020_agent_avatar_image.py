"""Add avatar_image_id to agents table.

Non-destructive migration: adds avatar_image_id alongside existing
avatar_url for backward compatibility. avatar_url is deprecated but
kept for existing data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020_agent_avatar_image"
down_revision = "019_channel_executor_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("avatar_image_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "avatar_image_id")
