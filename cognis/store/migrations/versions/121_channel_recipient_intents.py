"""Persist explicit channel recipient intents."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "121_channel_recipient_intents"
down_revision: str | Sequence[str] | None = "120_schedule_fire_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_recipient_intents",
        sa.Column("intent_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("channel_type", sa.String(), nullable=False),
        sa.Column("address_kind", sa.String(), nullable=False),
        sa.Column("normalized_address", sa.String(), nullable=False),
        sa.Column("chat_kind", sa.String(), nullable=False),
        sa.Column("allow_resolution", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("allow_creation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("provisional_route_key", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("conversation_id", sa.String(), server_default="", nullable=False),
        sa.Column("idempotency_key", sa.String(), server_default="", nullable=False),
        sa.Column("idempotency_scope", sa.String(), server_default="", nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("authorized_artifacts_json", sa.JSON(), nullable=True),
        sa.Column("resolution_lease_token", sa.String(), nullable=True),
        sa.Column("resolution_lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolution_state", sa.String(), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("side_effect_certainty", sa.String(), server_default="none", nullable=False),
        sa.Column("resolved_route_json", sa.JSON(), nullable=True),
        sa.Column("safe_error_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["channel_accounts.account_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("intent_id"),
        sa.UniqueConstraint("user_email", "intent_id", name="uq_channel_recipient_intent_owner"),
    )
    op.create_index(
        "ix_channel_recipient_intents_state",
        "channel_recipient_intents",
        ["resolution_state", "updated_at"],
    )
    op.create_index(
        "ix_channel_recipient_intents_route",
        "channel_recipient_intents",
        ["account_id", "provisional_route_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_channel_recipient_intents_route", table_name="channel_recipient_intents")
    op.drop_index("ix_channel_recipient_intents_state", table_name="channel_recipient_intents")
    op.drop_table("channel_recipient_intents")
