"""Add skill_id to schedules.

Revision ID: 041_schedule_skill_sources
Revises: 040_browser_sessions
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "041_schedule_skill_sources"
down_revision = "040_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("skill_id", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("skill_id")
