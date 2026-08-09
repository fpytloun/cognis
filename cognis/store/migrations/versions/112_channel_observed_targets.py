"""Add controller-observed channel targets."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "112_channel_observed_targets"
down_revision: str | Sequence[str] | None = "111_managed_channel_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "channel_observed_targets" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "channel_observed_targets",
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("user_email", sa.String(), nullable=False),
            sa.Column("account_id", sa.String(), nullable=False),
            sa.Column("channel_type", sa.String(), nullable=False),
            sa.Column("chat_id", sa.String(), nullable=False),
            sa.Column("chat_kind", sa.String(), nullable=False),
            sa.Column("display_name", sa.String(), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["account_id"], ["channel_accounts.account_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["user_email"], ["users.email"]),
            sa.PrimaryKeyConstraint("target_id"),
            sa.UniqueConstraint("account_id", "chat_id", name="uq_channel_observed_target"),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("channel_observed_targets")}
    if "ix_channel_observed_targets_owner" not in indexes:
        op.create_index(
            "ix_channel_observed_targets_owner",
            "channel_observed_targets",
            ["user_email", "last_observed_at"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_channel_observed_targets_owner", table_name="channel_observed_targets")
    op.drop_table("channel_observed_targets")
