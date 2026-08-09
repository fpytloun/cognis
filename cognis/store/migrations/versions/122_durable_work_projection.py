"""Add the durable Cognis Work projection."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "122_durable_work_projection"
down_revision: str | Sequence[str] | None = "121_channel_recipient_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_records",
        sa.Column("work_record_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("materializer_version", sa.String(), nullable=False),
        sa.Column("source_store", sa.String(), server_default="intaris", nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=False),
        sa.Column("source_seq", sa.BigInteger(), nullable=False),
        sa.Column("source_event_id", sa.String(), nullable=True),
        sa.Column("source_item_id", sa.String(), nullable=False),
        sa.Column("item_ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("record_type", sa.String(), nullable=False),
        sa.Column("is_evidence", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("pairing_key", sa.String(), nullable=True),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("timeline_item", sa.JSON(), nullable=False),
        sa.Column(
            "materialized_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_email"], ["users.email"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("work_record_id"),
        sa.UniqueConstraint(
            "owner_email",
            "source_store",
            "source_session_id",
            "source_seq",
            "item_ordinal",
            "materializer_version",
            name="uq_work_records_source_item_version",
        ),
    )
    op.create_index(
        "ix_work_records_owner_session_version_order",
        "work_records",
        [
            "owner_email",
            "session_id",
            "materializer_version",
            "occurred_at",
            "source_seq",
            "item_ordinal",
            "work_record_id",
        ],
    )
    op.create_index(
        "ix_work_records_owner_version_newest",
        "work_records",
        [
            "owner_email",
            "materializer_version",
            "is_evidence",
            "occurred_at",
            "session_id",
            "source_seq",
            "item_ordinal",
            "work_record_id",
        ],
    )
    op.create_index(
        "ix_work_records_pairing",
        "work_records",
        ["owner_email", "session_id", "materializer_version", "call_id", "pairing_key"],
    )
    op.create_table(
        "work_session_projections",
        sa.Column("projection_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("source_session_id", sa.String(), nullable=False),
        sa.Column("materializer_version", sa.String(), nullable=False),
        sa.Column("target_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("covered_through_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("materialized_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("head_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','materializing','caught_up','repair','failed')",
            name="ck_work_session_projections_state",
        ),
        sa.CheckConstraint(
            "target_seq >= 0 AND covered_through_seq >= 0 AND retry_count >= 0 "
            "AND lease_fence >= 0",
            name="ck_work_session_projections_nonnegative",
        ),
        sa.ForeignKeyConstraint(["owner_email"], ["users.email"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("projection_id"),
        sa.UniqueConstraint(
            "session_id", "materializer_version", name="uq_work_session_projections_session_version"
        ),
    )
    op.create_index(
        "ix_work_session_projections_queue",
        "work_session_projections",
        ["materializer_version", "state", "priority", "next_retry_at"],
    )
    op.create_index(
        "ix_work_session_projections_owner_state",
        "work_session_projections",
        ["owner_email", "materializer_version", "state"],
    )
    op.create_index(
        "ix_work_session_projections_lease",
        "work_session_projections",
        ["lease_expires_at", "lease_fence"],
    )


def downgrade() -> None:
    op.drop_table("work_session_projections")
    op.drop_table("work_records")
