"""Add durable ownership leases for follow-up execution.

Revision ID: 083_follow_up_leases
Revises: 082_sys_agent_profile_overrides
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "083_follow_up_leases"
down_revision: str | Sequence[str] | None = "082_sys_agent_profile_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def upgrade() -> None:
    for table_name in ("follow_up_intents", "follow_up_dedupe"):
        columns = _column_names(table_name)
        with op.batch_alter_table(table_name) as batch_op:
            if "lease_owner" not in columns:
                batch_op.add_column(sa.Column("lease_owner", sa.String(), nullable=True))
            if "lease_expires_at" not in columns:
                batch_op.add_column(
                    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
                )

    if "ix_follow_up_intents_lease" not in _index_names("follow_up_intents"):
        op.create_index(
            "ix_follow_up_intents_lease",
            "follow_up_intents",
            ["status", "lease_expires_at"],
        )
    if "ix_follow_up_dedupe_lease" not in _index_names("follow_up_dedupe"):
        op.create_index(
            "ix_follow_up_dedupe_lease",
            "follow_up_dedupe",
            ["status", "lease_expires_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_follow_up_dedupe_lease", table_name="follow_up_dedupe")
    op.drop_index("ix_follow_up_intents_lease", table_name="follow_up_intents")
    for table_name in ("follow_up_dedupe", "follow_up_intents"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column("lease_expires_at")
            batch_op.drop_column("lease_owner")
