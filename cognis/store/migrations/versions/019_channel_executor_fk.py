"""Add executor foreign key for channel accounts."""

from __future__ import annotations

from alembic import op

revision = "019_channel_executor_fk"
down_revision = "018_channel_adapter_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE channel_accounts
        SET executor_id = NULL
        WHERE executor_id IS NOT NULL
          AND executor_id NOT IN (SELECT executor_id FROM executors)
        """
    )
    with op.batch_alter_table("channel_accounts") as batch_op:
        batch_op.create_foreign_key(
            "fk_channel_accounts_executor_id",
            "executors",
            ["executor_id"],
            ["executor_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("channel_accounts") as batch_op:
        batch_op.drop_constraint("fk_channel_accounts_executor_id", type_="foreignkey")
