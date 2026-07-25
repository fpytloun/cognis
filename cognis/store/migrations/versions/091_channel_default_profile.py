"""Add channel-account default runtime profiles.

Revision ID: 091_channel_default_profile
Revises: 090_local_model_provider_domain
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "091_channel_default_profile"
down_revision: str | Sequence[str] | None = "090_local_model_provider_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    if "channel_accounts" not in inspector.get_table_names():
        return False
    return any(
        column["name"] == "default_agent_profile_id"
        for column in inspector.get_columns("channel_accounts")
    )


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table("channel_accounts") as batch_op:
        batch_op.add_column(sa.Column("default_agent_profile_id", sa.String(), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table("channel_accounts") as batch_op:
        batch_op.drop_column("default_agent_profile_id")
