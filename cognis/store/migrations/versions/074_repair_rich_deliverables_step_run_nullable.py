"""Repair Rich Deliverables direct-chat schema.

Revision ID: 074_repair_rich_deliverables_step_run_nullable
Revises: 073_rich_deliverables_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "074_repair_rich_deliverables_step_run_nullable"
down_revision = "073_rich_deliverables_v1"
branch_labels = None
depends_on = None


def _columns() -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("deliverables")
    }


def _unique_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints("deliverables")
    }


def _foreign_key_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_foreign_keys("deliverables")
        if constraint.get("name")
    }


def upgrade() -> None:
    columns = _columns()
    step_run_column = columns.get("step_run_id")
    if step_run_column is not None and step_run_column.get("nullable") is False:
        with op.batch_alter_table("deliverables") as batch:
            batch.alter_column("step_run_id", existing_type=sa.String(), nullable=True)

    foreign_keys = _foreign_key_names()
    if (
        "conversation_id" in columns
        and "fk_deliverables_conversation_id_conversations" not in foreign_keys
    ):
        with op.batch_alter_table("deliverables") as batch:
            batch.create_foreign_key(
                "fk_deliverables_conversation_id_conversations",
                "conversations",
                ["conversation_id"],
                ["conversation_id"],
                ondelete="CASCADE",
            )
    if "session_id" in columns and "fk_deliverables_session_id_sessions" not in foreign_keys:
        with op.batch_alter_table("deliverables") as batch:
            batch.create_foreign_key(
                "fk_deliverables_session_id_sessions",
                "sessions",
                ["session_id"],
                ["session_id"],
                ondelete="CASCADE",
            )

    if "uq_deliverables_conversation_scope_version" not in _unique_constraint_names():
        with op.batch_alter_table("deliverables") as batch:
            batch.create_unique_constraint(
                "uq_deliverables_conversation_scope_version",
                ["conversation_id", "session_id", "turn_id", "version"],
            )


def downgrade() -> None:
    # The repaired shape is now owned by 073_rich_deliverables_v1. Downgrading
    # this compatibility migration alone should not remove columns,
    # constraints, or nullability that 073 currently declares.
    pass
