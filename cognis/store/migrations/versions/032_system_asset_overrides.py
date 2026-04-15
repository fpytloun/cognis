"""Add per-user system agent and workflow overrides.

Revision ID: 032_system_asset_overrides
Revises: 031_skill_is_system
Create Date: 2026-04-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "032_system_asset_overrides"
down_revision = "031_skill_is_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_agent_overrides",
        sa.Column("override_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("llm_config_override", sa.JSON(), nullable=True),
        sa.Column("execution_override", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_email"], ["users.email"]),
        sa.PrimaryKeyConstraint("override_id"),
        sa.UniqueConstraint(
            "owner_email", "agent_id", name="uq_system_agent_overrides_owner_agent"
        ),
    )
    op.create_index(
        "ix_system_agent_overrides_owner", "system_agent_overrides", ["owner_email"], unique=False
    )

    op.create_table(
        "system_workflow_overrides",
        sa.Column("override_id", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("step_overrides", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_email"], ["users.email"]),
        sa.PrimaryKeyConstraint("override_id"),
        sa.UniqueConstraint(
            "owner_email", "workflow_id", name="uq_system_workflow_overrides_owner_workflow"
        ),
    )
    op.create_index(
        "ix_system_workflow_overrides_owner",
        "system_workflow_overrides",
        ["owner_email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_system_workflow_overrides_owner", table_name="system_workflow_overrides")
    op.drop_table("system_workflow_overrides")
    op.drop_index("ix_system_agent_overrides_owner", table_name="system_agent_overrides")
    op.drop_table("system_agent_overrides")
