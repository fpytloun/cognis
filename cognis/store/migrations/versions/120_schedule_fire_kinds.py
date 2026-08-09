"""Separate recurring and manual schedule fire identities.

Revision ID: 120_schedule_fire_kinds
Revises: 119_work_scope_revisions
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "120_schedule_fire_kinds"
down_revision: str | Sequence[str] | None = "119_work_scope_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedule_fires") as batch_op:
        batch_op.add_column(
            sa.Column(
                "fire_kind",
                sa.String(),
                server_default="recurring",
                nullable=False,
            )
        )
        batch_op.drop_constraint("uq_schedule_fires_logical_fire", type_="unique")
        batch_op.create_unique_constraint(
            "uq_schedule_fires_logical_fire",
            ["schedule_id", "fire_kind", "scheduled_fire_at"],
        )
        batch_op.create_check_constraint(
            "ck_schedule_fires_fire_kind",
            "fire_kind IN ('recurring', 'manual')",
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM schedule_fires WHERE fire_kind = 'manual'"))
    with op.batch_alter_table("schedule_fires") as batch_op:
        batch_op.drop_constraint("ck_schedule_fires_fire_kind", type_="check")
        batch_op.drop_constraint("uq_schedule_fires_logical_fire", type_="unique")
        batch_op.create_unique_constraint(
            "uq_schedule_fires_logical_fire",
            ["schedule_id", "scheduled_fire_at"],
        )
        batch_op.drop_column("fire_kind")
