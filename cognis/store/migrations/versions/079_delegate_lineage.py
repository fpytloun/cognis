"""Add lightweight delegate lineage metadata.

Revision ID: 079_delegate_lineage
Revises: 078_follow_up_intents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "079_delegate_lineage"
down_revision: str | Sequence[str] | None = "078_follow_up_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sessions")}
    if "delegation_metadata" not in columns:
        with op.batch_alter_table("sessions") as batch_op:
            batch_op.add_column(sa.Column("delegation_metadata", sa.JSON(), nullable=True))
    op.execute("UPDATE sessions SET delegation_metadata = '{}' WHERE delegation_metadata IS NULL")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column("delegation_metadata", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("delegation_metadata")
