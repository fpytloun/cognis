"""Add durable direct-turn request admission state.

Revision ID: 097_direct_turn_requests
Revises: 096_coordination_leases
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "097_direct_turn_requests"
down_revision: str | None = "096_coordination_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "direct_turn_requests",
        sa.Column(
            "admission_order",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("idempotency_scope", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("admission_hash", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("owner_controller_id", sa.String(), nullable=True),
        sa.Column("owner_incarnation_id", sa.String(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("cancel_requested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("absorbed_by_turn_id", sa.String(), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ("
            "'queued', 'claimed', 'running', 'absorbing', 'recoverable', "
            "'completed', 'failed', 'cancelled', 'absorbed', 'ambiguous'"
            ")",
            name="ck_direct_turn_requests_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("admission_order"),
        sa.UniqueConstraint(
            "idempotency_scope",
            "idempotency_key",
            name="uq_direct_turn_requests_idempotency",
        ),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("turn_id"),
    )
    op.create_index(
        "ix_direct_turn_requests_fifo",
        "direct_turn_requests",
        ["conversation_id", "status", "admission_order"],
    )
    op.create_index(
        "ix_direct_turn_requests_owner",
        "direct_turn_requests",
        ["owner_controller_id", "owner_incarnation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_direct_turn_requests_owner", table_name="direct_turn_requests")
    op.drop_index("ix_direct_turn_requests_fifo", table_name="direct_turn_requests")
    op.drop_table("direct_turn_requests")
