"""Add durable destination-scoped Signal admission."""

import sqlalchemy as sa
from alembic import op

revision = "149_signal_destination_policy"
down_revision = "148_channel_delivery_route_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("signal_destination_policies"):
        op.create_table(
            "signal_destination_policies",
            sa.Column(
                "account_id",
                sa.String(),
                sa.ForeignKey("channel_accounts.account_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("destination", sa.String(), primary_key=True),
            sa.Column("user_email", sa.String(), nullable=False),
            sa.Column("state_json", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("signal_destination_policies")
