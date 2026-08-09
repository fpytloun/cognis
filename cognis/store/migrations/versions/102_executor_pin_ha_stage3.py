"""Add Stage 3 executor pin generations, transition ledger, and notice outbox.

Revision ID: 102_executor_pin_ha_stage3
Revises: 101_mcp_oauth_cleanup_dispatch
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "102_executor_pin_ha_stage3"
down_revision: str | None = "101_mcp_oauth_cleanup_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "active_executor_generation", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("active_executor_unavailable_since", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "active_executor_generation", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "tasks",
        sa.Column("active_executor_unavailable_since", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_table(
        "executor_pin_transitions",
        sa.Column("transition_id", sa.String(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("old_executor_id", sa.String(), nullable=True),
        sa.Column("new_executor_id", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state_caveat", sa.Text(), nullable=False),
        sa.Column("notice_id", sa.String(), nullable=False),
        sa.Column("notice_appended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint("notice_id"),
        sa.UniqueConstraint(
            "scope_type", "scope_id", "generation", name="uq_executor_pin_transition_generation"
        ),
    )
    op.create_index(
        "ix_executor_pin_transitions_notice_pending",
        "executor_pin_transitions",
        ["notice_appended_at"],
    )
    op.create_table(
        "executor_pin_notice_outbox",
        sa.Column("outbox_id", sa.String(), nullable=False),
        sa.Column("transition_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("intaris_session_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["transition_id"], ["executor_pin_transitions.transition_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint("transition_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_executor_pin_notice_outbox_pending", "executor_pin_notice_outbox", ["delivered_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_executor_pin_notice_outbox_pending", table_name="executor_pin_notice_outbox")
    op.drop_table("executor_pin_notice_outbox")
    op.drop_index(
        "ix_executor_pin_transitions_notice_pending", table_name="executor_pin_transitions"
    )
    op.drop_table("executor_pin_transitions")
    op.drop_column("tasks", "active_executor_generation")
    op.drop_column("tasks", "active_executor_unavailable_since")
    op.drop_column("conversations", "active_executor_generation")
    op.drop_column("conversations", "active_executor_unavailable_since")
