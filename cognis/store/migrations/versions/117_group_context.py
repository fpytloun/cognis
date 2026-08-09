"""Add bounded group-context ordering and admission metadata.

Revision ID: 117_group_context
Revises: 116_managed_resume_prepared
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "117_group_context"
down_revision: str | Sequence[str] | None = "116_managed_resume_prepared"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ledger_columns = {column["name"] for column in inspector.get_columns("channel_inbound_ledger")}
    with op.batch_alter_table("channel_inbound_ledger") as batch:
        if "observed_at" not in ledger_columns:
            batch.add_column(sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True))
        if "ordering_key" not in ledger_columns:
            batch.add_column(sa.Column("ordering_key", sa.String(), nullable=True))
        if "ordering_source" not in ledger_columns:
            batch.add_column(
                sa.Column(
                    "ordering_source",
                    sa.String(),
                    server_default="observed",
                    nullable=False,
                )
            )
        if "retain_until" not in ledger_columns:
            batch.add_column(sa.Column("retain_until", sa.TIMESTAMP(timezone=True), nullable=True))
    op.execute(
        "UPDATE channel_inbound_ledger "
        "SET observed_at = created_at, "
        "ordering_key = COALESCE(CAST(occurred_at AS VARCHAR), '') || ':' || inbound_id "
        "WHERE observed_at IS NULL OR ordering_key IS NULL"
    )
    inspector = sa.inspect(bind)
    ledger_indexes = {
        index["name"]: tuple(index.get("column_names") or ())
        for index in inspector.get_indexes("channel_inbound_ledger")
    }
    context_index_columns = (
        "account_id",
        "chat_id",
        "thread_key",
        "ordering_key",
        "message_id",
    )
    with op.batch_alter_table("channel_inbound_ledger") as batch:
        batch.alter_column("observed_at", existing_type=sa.TIMESTAMP(timezone=True), nullable=False)
        batch.alter_column("ordering_key", existing_type=sa.String(), nullable=False)
        if ledger_indexes.get("ix_channel_inbound_ledger_context") != context_index_columns:
            if "ix_channel_inbound_ledger_context" in ledger_indexes:
                batch.drop_index("ix_channel_inbound_ledger_context")
            batch.create_index(
                "ix_channel_inbound_ledger_context",
                list(context_index_columns),
            )
        if "ix_channel_inbound_ledger_retention" not in ledger_indexes:
            batch.create_index(
                "ix_channel_inbound_ledger_retention",
                ["binding_id", "retain_until"],
            )

    inspector = sa.inspect(bind)
    consumption_columns = {
        column["name"] for column in inspector.get_columns("channel_context_consumptions")
    }
    consumption_indexes = {
        index["name"] for index in inspector.get_indexes("channel_context_consumptions")
    }
    with op.batch_alter_table("channel_context_consumptions") as batch:
        if "usage" not in consumption_columns:
            batch.add_column(
                sa.Column("usage", sa.String(), server_default="context", nullable=False)
            )
        if "trigger_inbound_id" not in consumption_columns:
            batch.add_column(sa.Column("trigger_inbound_id", sa.String(), nullable=True))
        if "admitted_turn_id" not in consumption_columns:
            batch.add_column(sa.Column("admitted_turn_id", sa.String(), nullable=True))
        if "ix_channel_context_consumptions_admission" not in consumption_indexes:
            batch.create_index(
                "ix_channel_context_consumptions_admission",
                ["admitted_turn_id", "state"],
            )


def downgrade() -> None:
    with op.batch_alter_table("channel_context_consumptions") as batch:
        batch.drop_index("ix_channel_context_consumptions_admission")
        batch.drop_column("admitted_turn_id")
        batch.drop_column("trigger_inbound_id")
        batch.drop_column("usage")
    with op.batch_alter_table("channel_inbound_ledger") as batch:
        batch.drop_index("ix_channel_inbound_ledger_retention")
        batch.drop_index("ix_channel_inbound_ledger_context")
        batch.create_index(
            "ix_channel_inbound_ledger_context",
            ["account_id", "chat_id", "thread_key", "occurred_at"],
        )
        batch.drop_column("retain_until")
        batch.drop_column("ordering_source")
        batch.drop_column("ordering_key")
        batch.drop_column("observed_at")
