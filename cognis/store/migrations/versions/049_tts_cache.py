"""Add TTS audio cache metadata table.

Revision ID: 049_tts_cache
Revises: 048_task_interaction_overrides
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "049_tts_cache"
down_revision = "048_task_interaction_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tts_cache",
        sa.Column("message_id", sa.String(), primary_key=True),
        sa.Column("voice", sa.String(), primary_key=True),
        sa.Column("model", sa.String(), primary_key=True),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("artifact_filename", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_tts_cache_created_at", "tts_cache", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_tts_cache_created_at", table_name="tts_cache")
    op.drop_table("tts_cache")
