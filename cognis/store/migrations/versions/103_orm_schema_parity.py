"""Restore additive Alembic and ORM schema parity.

Revision ID: 103_orm_schema_parity
Revises: 102_executor_pin_ha_stage3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "103_orm_schema_parity"
down_revision = "102_executor_pin_ha_stage3"
branch_labels = None
depends_on = None


def _column_map(table_name: str) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    agent_columns = _column_map("agents")
    if "agent_type" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column(
                "agent_type",
                sa.String(20),
                nullable=False,
                server_default="primary",
            ),
        )
    if "is_system" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column(
                "is_system",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "hidden" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column(
                "hidden",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    inspector = sa.inspect(op.get_bind())
    if "agent_secondary_bindings" not in inspector.get_table_names():
        op.create_table(
            "agent_secondary_bindings",
            sa.Column(
                "primary_agent_id",
                sa.String(),
                sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "secondary_agent_id",
                sa.String(),
                sa.ForeignKey("agents.agent_id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )

    for table_name in ("users", "artifacts"):
        columns = _column_map(table_name)
        updated_at = columns.get("updated_at")
        if updated_at is None or not updated_at["nullable"]:
            continue
        op.execute(
            sa.text(f"UPDATE {table_name} SET updated_at = created_at WHERE updated_at IS NULL")
        )
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                "updated_at",
                existing_type=sa.TIMESTAMP(timezone=True),
                nullable=False,
            )


def downgrade() -> None:
    # This migration repairs objects that may already have been created and
    # populated by the supported bootstrap path. A destructive downgrade could
    # not distinguish migration-owned objects from bootstrap-owned objects, so
    # preserve the additive schema and data.
    pass
