"""managed channel conversation foundation

Revision ID: 111_managed_channel_foundation
Revises: 110_conversation_lineage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "111_managed_channel_foundation"
down_revision = "110_conversation_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    link_columns = {
        column["name"] for column in inspector.get_columns("managed_conversation_links")
    }
    link_indexes = {index["name"] for index in inspector.get_indexes("managed_conversation_links")}
    with op.batch_alter_table("managed_conversation_links") as batch_op:
        if "kind" not in link_columns:
            batch_op.add_column(
                sa.Column("kind", sa.String(), server_default="agent", nullable=False)
            )
        if "completion_policy" not in link_columns:
            batch_op.add_column(
                sa.Column(
                    "completion_policy",
                    sa.String(),
                    server_default="turn",
                    nullable=False,
                )
            )
        if "owner_epoch" not in link_columns:
            batch_op.add_column(
                sa.Column("owner_epoch", sa.BigInteger(), server_default="1", nullable=False)
            )
        if "creation_policy_snapshot" not in link_columns:
            batch_op.add_column(sa.Column("creation_policy_snapshot", sa.JSON(), nullable=True))
        if "ix_managed_conversation_links_user_kind_state" not in link_indexes:
            batch_op.create_index(
                "ix_managed_conversation_links_user_kind_state",
                ["user_email", "kind", "conversation_state"],
            )

    tables = set(sa.inspect(bind).get_table_names())
    if "managed_conversation_signals" not in tables:
        op.create_table(
            "managed_conversation_signals",
            sa.Column("signal_id", sa.String(), primary_key=True),
            sa.Column(
                "link_id",
                sa.String(),
                sa.ForeignKey("managed_conversation_links.link_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("owner_epoch", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(), server_default="explicit", nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("wait", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("state", sa.String(), server_default="pending", nullable=False),
            sa.Column("memory_eligible", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
    signal_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("managed_conversation_signals")
    }
    if "ix_managed_conversation_signals_link_state" not in signal_indexes:
        op.create_index(
            "ix_managed_conversation_signals_link_state",
            "managed_conversation_signals",
            ["link_id", "state"],
        )
    if "ix_managed_conversation_signals_owner" not in signal_indexes:
        op.create_index(
            "ix_managed_conversation_signals_owner",
            "managed_conversation_signals",
            ["link_id", "owner_epoch"],
        )

    if "managed_channel_bindings" not in tables:
        op.create_table(
            "managed_channel_bindings",
            sa.Column("binding_id", sa.String(), primary_key=True),
            sa.Column(
                "link_id",
                sa.String(),
                sa.ForeignKey("managed_conversation_links.link_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
            sa.Column(
                "account_id",
                sa.String(),
                sa.ForeignKey("channel_accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("chat_id", sa.String(), nullable=False),
            sa.Column("thread_key", sa.String(), server_default="", nullable=False),
            sa.Column("sender_id", sa.String(), nullable=False),
            sa.Column("active_route_key", sa.String(), nullable=True),
            sa.Column("state", sa.String(), server_default="provisioning", nullable=False),
            sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
            sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("safety_guidance", sa.Text(), server_default="", nullable=False),
            sa.Column("explicit_tool_allowlist", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("terminal_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.UniqueConstraint("link_id", name="uq_managed_channel_bindings_link"),
            sa.UniqueConstraint(
                "active_route_key", name="uq_managed_channel_bindings_active_route"
            ),
        )
    binding_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("managed_channel_bindings")
    }
    if "ix_managed_channel_bindings_user_state" not in binding_indexes:
        op.create_index(
            "ix_managed_channel_bindings_user_state",
            "managed_channel_bindings",
            ["user_email", "state"],
        )
    if "ix_managed_channel_bindings_expires" not in binding_indexes:
        op.create_index(
            "ix_managed_channel_bindings_expires",
            "managed_channel_bindings",
            ["expires_at"],
        )

    if "channel_inbound_ledger" not in tables:
        op.create_table(
            "channel_inbound_ledger",
            sa.Column("inbound_id", sa.String(), primary_key=True),
            sa.Column("user_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
            sa.Column(
                "account_id",
                sa.String(),
                sa.ForeignKey("channel_accounts.account_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "binding_id",
                sa.String(),
                sa.ForeignKey("managed_channel_bindings.binding_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("channel_type", sa.String(), nullable=False),
            sa.Column("chat_id", sa.String(), nullable=False),
            sa.Column("thread_key", sa.String(), server_default="", nullable=False),
            sa.Column("message_id", sa.String(), nullable=False),
            sa.Column("sender_id", sa.String(), nullable=False),
            sa.Column("sender_name", sa.String(), nullable=True),
            sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("is_bot_output", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("is_primary_input", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("disposition", sa.String(), server_default="pending", nullable=False),
            sa.Column("platform_data", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "account_id",
                "chat_id",
                "thread_key",
                "message_id",
                name="uq_channel_inbound_ledger_message",
            ),
        )
    inbound_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("channel_inbound_ledger")
    }
    if "ix_channel_inbound_ledger_context" not in inbound_indexes:
        op.create_index(
            "ix_channel_inbound_ledger_context",
            "channel_inbound_ledger",
            ["account_id", "chat_id", "thread_key", "occurred_at"],
        )
    if "ix_channel_inbound_ledger_binding_state" not in inbound_indexes:
        op.create_index(
            "ix_channel_inbound_ledger_binding_state",
            "channel_inbound_ledger",
            ["binding_id", "disposition"],
        )

    if "channel_context_consumptions" not in tables:
        op.create_table(
            "channel_context_consumptions",
            sa.Column("consumption_id", sa.String(), primary_key=True),
            sa.Column(
                "consumer_conversation_id",
                sa.String(),
                sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "inbound_id",
                sa.String(),
                sa.ForeignKey("channel_inbound_ledger.inbound_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("state", sa.String(), server_default="reserved", nullable=False),
            sa.Column("reservation_token", sa.String(), nullable=False),
            sa.Column("reserved_until", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("committed_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "consumer_conversation_id",
                "inbound_id",
                name="uq_channel_context_consumptions_consumer_inbound",
            ),
        )
    consumption_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("channel_context_consumptions")
    }
    if "ix_channel_context_consumptions_reservation" not in consumption_indexes:
        op.create_index(
            "ix_channel_context_consumptions_reservation",
            "channel_context_consumptions",
            ["state", "reserved_until"],
        )


def downgrade() -> None:
    op.drop_table("channel_context_consumptions")
    op.drop_table("channel_inbound_ledger")
    op.drop_table("managed_channel_bindings")
    op.drop_table("managed_conversation_signals")
    with op.batch_alter_table("managed_conversation_links") as batch_op:
        batch_op.drop_index("ix_managed_conversation_links_user_kind_state")
        batch_op.drop_column("creation_policy_snapshot")
        batch_op.drop_column("owner_epoch")
        batch_op.drop_column("completion_policy")
        batch_op.drop_column("kind")
