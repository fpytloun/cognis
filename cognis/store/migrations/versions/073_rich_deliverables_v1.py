"""Add Rich Deliverables v1 storage fields.

Revision ID: 073_rich_deliverables_v1
Revises: 072_conversation_sidebar_indexes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "073_rich_deliverables_v1"
down_revision = "072_conversation_sidebar_indexes"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("deliverables")}


def _index_names() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("deliverables")}


def _unique_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints("deliverables")
    }


def upgrade() -> None:
    columns = _column_names()
    with op.batch_alter_table("deliverables") as batch:
        batch.alter_column("step_run_id", existing_type=sa.String(), nullable=True)
        if "conversation_id" not in columns:
            batch.add_column(
                sa.Column(
                    "conversation_id",
                    sa.String(),
                    sa.ForeignKey(
                        "conversations.conversation_id",
                        ondelete="CASCADE",
                        name="fk_deliverables_conversation_id_conversations",
                    ),
                    nullable=True,
                )
            )
        if "session_id" not in columns:
            batch.add_column(
                sa.Column(
                    "session_id",
                    sa.String(),
                    sa.ForeignKey(
                        "sessions.session_id",
                        ondelete="CASCADE",
                        name="fk_deliverables_session_id_sessions",
                    ),
                    nullable=True,
                )
            )
        if "turn_id" not in columns:
            batch.add_column(sa.Column("turn_id", sa.String(), nullable=True))
        if "rich_payload" not in columns:
            batch.add_column(sa.Column("rich_payload", sa.JSON(), nullable=True))
        if "validation_warnings" not in columns:
            batch.add_column(sa.Column("validation_warnings", sa.JSON(), nullable=True))
        if "render_metadata" not in columns:
            batch.add_column(sa.Column("render_metadata", sa.JSON(), nullable=True))
        if "export_metadata" not in columns:
            batch.add_column(sa.Column("export_metadata", sa.JSON(), nullable=True))

    if "ix_deliverables_conversation_scope" not in _index_names():
        op.create_index(
            "ix_deliverables_conversation_scope",
            "deliverables",
            ["conversation_id", "session_id", "turn_id"],
        )

    if "uq_deliverables_conversation_scope_version" not in _unique_constraint_names():
        with op.batch_alter_table("deliverables") as batch:
            batch.create_unique_constraint(
                "uq_deliverables_conversation_scope_version",
                ["conversation_id", "session_id", "turn_id", "version"],
            )


def downgrade() -> None:
    if "uq_deliverables_conversation_scope_version" in _unique_constraint_names():
        with op.batch_alter_table("deliverables") as batch:
            batch.drop_constraint(
                "uq_deliverables_conversation_scope_version",
                type_="unique",
            )

    if "ix_deliverables_conversation_scope" in _index_names():
        op.drop_index("ix_deliverables_conversation_scope", table_name="deliverables")

    columns = _column_names()
    op.execute(sa.text("DELETE FROM deliverables WHERE step_run_id IS NULL"))
    with op.batch_alter_table("deliverables") as batch:
        for name in (
            "export_metadata",
            "render_metadata",
            "validation_warnings",
            "rich_payload",
            "turn_id",
            "session_id",
            "conversation_id",
        ):
            if name in columns:
                batch.drop_column(name)
        batch.alter_column("step_run_id", existing_type=sa.String(), nullable=False)
