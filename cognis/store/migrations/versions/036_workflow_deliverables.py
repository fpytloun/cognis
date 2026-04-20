"""Add workflow deliverables table and step run deliverable columns.

Revision ID: 036_workflow_deliverables
Revises: 035_harness_recovery_tables
Create Date: 2026-04-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "036_workflow_deliverables"
down_revision = "035_harness_recovery_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deliverables",
        sa.Column("deliverable_id", sa.String(), primary_key=True),
        sa.Column(
            "step_run_id",
            sa.String(),
            sa.ForeignKey("step_runs.step_run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("format", sa.String(), nullable=False, server_default="markdown"),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("outputs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="buffered"),
        sa.Column("evaluator_feedback", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("step_run_id", "version", name="uq_deliverables_step_run_version"),
    )
    op.create_index("ix_deliverables_step_run", "deliverables", ["step_run_id"])
    op.create_index("ix_deliverables_status", "deliverables", ["status"])

    with op.batch_alter_table("step_runs") as batch:
        batch.add_column(sa.Column("deliverable_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("require_deliverable", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("step_runs") as batch:
        batch.drop_column("require_deliverable")
        batch.drop_column("deliverable_id")

    op.drop_index("ix_deliverables_status", table_name="deliverables")
    op.drop_index("ix_deliverables_step_run", table_name="deliverables")
    op.drop_table("deliverables")
