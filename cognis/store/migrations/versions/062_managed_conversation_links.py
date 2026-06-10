"""Add managed agent conversation links.

Revision ID: 062_managed_conversation_links
Revises: 061_mcp_timeout_settings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "062_managed_conversation_links"
down_revision = "061_mcp_timeout_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_conversation_links",
        sa.Column("link_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("controller_agent_id", sa.String(), nullable=False),
        sa.Column("controller_conversation_id", sa.String(), nullable=False),
        sa.Column("controller_session_id", sa.String(), nullable=True),
        sa.Column("target_agent_id", sa.String(), nullable=False),
        sa.Column("target_conversation_id", sa.String(), nullable=False),
        sa.Column("target_session_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("conversation_state", sa.String(), server_default="open", nullable=False),
        sa.Column("turn_state", sa.String(), server_default="idle", nullable=False),
        sa.Column("active_turn_id", sa.String(), nullable=True),
        sa.Column("notify_on_completion", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("last_result_summary", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("control_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["controller_agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(["controller_conversation_id"], ["conversations.conversation_id"]),
        sa.ForeignKeyConstraint(["target_agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(["target_conversation_id"], ["conversations.conversation_id"]),
        sa.ForeignKeyConstraint(["user_email"], ["users.email"]),
        sa.PrimaryKeyConstraint("link_id"),
        sa.UniqueConstraint(
            "target_conversation_id",
            name="uq_managed_conversation_links_target_conversation",
        ),
    )
    op.create_index(
        "ix_managed_conversation_links_controller_conversation",
        "managed_conversation_links",
        ["controller_conversation_id"],
    )
    op.create_index(
        "ix_managed_conversation_links_user_state",
        "managed_conversation_links",
        ["user_email", "conversation_state"],
    )
    op.create_index(
        "ix_managed_conversation_links_target_agent",
        "managed_conversation_links",
        ["target_agent_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_managed_conversation_links_target_agent",
        table_name="managed_conversation_links",
    )
    op.drop_index(
        "ix_managed_conversation_links_user_state",
        table_name="managed_conversation_links",
    )
    op.drop_index(
        "ix_managed_conversation_links_controller_conversation",
        table_name="managed_conversation_links",
    )
    op.drop_table("managed_conversation_links")
