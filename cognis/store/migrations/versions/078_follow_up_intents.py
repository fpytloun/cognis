"""Add durable follow-up intents.

Revision ID: 078_follow_up_intents
Revises: 077_managed_conversation_lineage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "078_follow_up_intents"
down_revision = "077_managed_conversation_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "follow_up_intents" not in inspector.get_table_names():
        op.create_table(
            "follow_up_intents",
            sa.Column("intent_id", sa.String(), nullable=False),
            sa.Column("conversation_id", sa.String(), nullable=False),
            sa.Column("follow_up_id", sa.String(), nullable=False),
            sa.Column("event_payload", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("intent_id"),
            sa.UniqueConstraint(
                "conversation_id",
                "follow_up_id",
                name="uq_follow_up_intents_pair",
            ),
        )
        op.create_index(
            "ix_follow_up_intents_status_updated",
            "follow_up_intents",
            ["status", "updated_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_follow_up_intents_status_updated", table_name="follow_up_intents")
    op.drop_table("follow_up_intents")
