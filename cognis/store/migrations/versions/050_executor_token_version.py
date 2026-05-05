"""Add revokable executor token version.

Revision ID: 050_executor_token_version
Revises: 049_tts_cache
Create Date: 2026-05-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "050_executor_token_version"
down_revision = "049_tts_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executors",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("executors", "token_version")
