"""Add rebuildable Work scope revisions and stream membership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "119_work_scope_revisions"
down_revision: str | Sequence[str] | None = "118_channel_delivery_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_sessions_owner_parent_session",
        "sessions",
        ["user_email", "parent_session_id", "session_id"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_owner_previous_session",
        "sessions",
        ["user_email", "previous_session_id", "session_id"],
        unique=False,
    )
    op.create_index(
        "ix_managed_conversation_links_owner_controller_session",
        "managed_conversation_links",
        ["user_email", "controller_session_id", "link_id"],
        unique=False,
    )
    op.create_table(
        "work_scope_states",
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("root_id", sa.String(), nullable=False),
        sa.Column("graph_fingerprint", sa.String(), nullable=True),
        sa.Column("work_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("graph_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("scope_key"),
    )
    op.create_index(
        "ix_work_scope_states_owner_root",
        "work_scope_states",
        ["user_email", "scope_kind", "root_id"],
        unique=False,
    )
    op.create_table(
        "work_scope_streams",
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("event_store_id", sa.String(), nullable=False),
        sa.Column("event_store_session_id", sa.String(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_key"],
            ["work_scope_states.scope_key"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("scope_key", "session_id"),
    )
    op.create_index(
        "ix_work_scope_streams_event_stream",
        "work_scope_streams",
        ["event_store_id", "event_store_session_id", "scope_key"],
        unique=False,
    )
    op.create_index(
        "ix_work_scope_streams_session",
        "work_scope_streams",
        ["session_id", "scope_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_work_scope_streams_session", table_name="work_scope_streams")
    op.drop_index("ix_work_scope_streams_event_stream", table_name="work_scope_streams")
    op.drop_table("work_scope_streams")
    op.drop_index("ix_work_scope_states_owner_root", table_name="work_scope_states")
    op.drop_table("work_scope_states")
    op.drop_index(
        "ix_managed_conversation_links_owner_controller_session",
        table_name="managed_conversation_links",
    )
    op.drop_index("ix_sessions_owner_previous_session", table_name="sessions")
    op.drop_index("ix_sessions_owner_parent_session", table_name="sessions")
