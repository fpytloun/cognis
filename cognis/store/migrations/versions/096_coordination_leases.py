"""Add the shared coordination lease primitive.

Revision ID: 096_coordination_leases
Revises: 095_channel_delivery_dlv_id
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "096_coordination_leases"
down_revision: str | None = "095_channel_delivery_dlv_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coordination_leases",
        sa.Column("resource_key", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("resource_key"),
    )
    op.create_index(
        "ix_coordination_leases_expires",
        "coordination_leases",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_coordination_leases_expires", table_name="coordination_leases")
    op.drop_table("coordination_leases")
