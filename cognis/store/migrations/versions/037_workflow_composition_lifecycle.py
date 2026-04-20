"""Add workflow lifecycle and skill decomposition fields.

Revision ID: 037_workflow_composition_lifecycle
Revises: 036_workflow_deliverables
Create Date: 2026-04-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "037_workflow_composition_lifecycle"
down_revision = "036_workflow_deliverables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(
            sa.Column("lifecycle", sa.String(), nullable=False, server_default="persistent")
        )
        batch.add_column(sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True))

    with op.batch_alter_table("skill_versions") as batch:
        batch.add_column(sa.Column("steps", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("decomposition_source_hash", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("skill_versions") as batch:
        batch.drop_column("decomposition_source_hash")
        batch.drop_column("steps")

    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("archived_at")
        batch.drop_column("lifecycle")
