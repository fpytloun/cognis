"""Add managed-conversation settlement turn correlation.

Revision ID: 080_managed_turn_correlation
Revises: 079_delegate_lineage
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "080_managed_turn_correlation"
down_revision: str | Sequence[str] | None = "079_delegate_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("managed_conversation_links")
    }
    if "last_result_turn_id" not in columns:
        with op.batch_alter_table("managed_conversation_links") as batch_op:
            batch_op.add_column(sa.Column("last_result_turn_id", sa.String(), nullable=True))


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("managed_conversation_links")
    }
    if "last_result_turn_id" in columns:
        with op.batch_alter_table("managed_conversation_links") as batch_op:
            batch_op.drop_column("last_result_turn_id")
