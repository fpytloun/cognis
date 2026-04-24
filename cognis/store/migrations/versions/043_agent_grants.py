"""Add agent grants table.

Revision ID: 043_agent_grants
Revises: 042_skill_linked_tool_ids
Create Date: 2026-04-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "043_agent_grants"
down_revision = "042_skill_linked_tool_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_grants",
        sa.Column("grant_id", sa.String(), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(),
            sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grantee_type", sa.String(), nullable=False, server_default="user"),
        sa.Column(
            "grantee_user_email",
            sa.String(),
            sa.ForeignKey("users.email"),
            nullable=True,
        ),
        sa.Column("grantee_group_id", sa.String(), nullable=True),
        sa.Column("permission", sa.String(), nullable=False, server_default="use"),
        sa.Column(
            "executor_scope",
            sa.String(),
            nullable=False,
            server_default="owner_executor",
        ),
        sa.Column("granted_by", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column(
            "granted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_agent_grants_grantee_user", "agent_grants", ["grantee_user_email"])
    op.create_index("ix_agent_grants_agent", "agent_grants", ["agent_id"])
    op.create_index(
        "uq_agent_grants_active_user",
        "agent_grants",
        ["agent_id", "grantee_user_email"],
        unique=True,
        sqlite_where=sa.text("grantee_type = 'user' AND revoked_at IS NULL"),
        postgresql_where=sa.text("grantee_type = 'user' AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_grants_active_user", table_name="agent_grants")
    op.drop_index("ix_agent_grants_agent", table_name="agent_grants")
    op.drop_index("ix_agent_grants_grantee_user", table_name="agent_grants")
    op.drop_table("agent_grants")
