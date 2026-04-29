"""Add task and schedule interaction overrides.

Revision ID: 048_task_interaction_overrides
Revises: 047_channel_preferred_delivery
Create Date: 2026-04-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "048_task_interaction_overrides"
down_revision = "047_channel_preferred_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("interaction_mode_override", sa.String(), nullable=True))
    op.add_column(
        "schedules",
        sa.Column(
            "interaction_mode_override",
            sa.String(),
            nullable=True,
            server_default="none",
        ),
    )


def downgrade() -> None:
    op.drop_column("schedules", "interaction_mode_override")
    op.drop_column("tasks", "interaction_mode_override")
