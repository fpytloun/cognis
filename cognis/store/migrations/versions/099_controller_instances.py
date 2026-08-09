"""Add controller instance directory.

Revision ID: 099_controller_instances
Revises: 098_schedule_fires
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "099_controller_instances"
down_revision: str | None = "098_schedule_fires"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "controller_instances",
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("controller_id", sa.String(), nullable=False),
        sa.Column("incarnation_id", sa.String(), nullable=False),
        sa.Column("internal_url", sa.String(), nullable=True),
        sa.Column("lifecycle_state", sa.String(), nullable=False),
        sa.Column("heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "lifecycle_state IN ('starting', 'ready', 'draining', 'stopped')",
            name="ck_controller_instances_lifecycle_state",
        ),
        sa.PrimaryKeyConstraint("owner_id"),
    )
    op.create_index(
        "ix_controller_instances_controller",
        "controller_instances",
        ["controller_id"],
    )
    op.create_index(
        "ix_controller_instances_expires",
        "controller_instances",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_controller_instances_expires", table_name="controller_instances")
    op.drop_index("ix_controller_instances_controller", table_name="controller_instances")
    op.drop_table("controller_instances")
