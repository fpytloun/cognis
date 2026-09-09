"""Persist interactive session runtime overrides.

Revision ID: 147_session_runtime_overrides
Revises: 146_schedule_fire_timezone
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "147_session_runtime_overrides"
down_revision = "146_schedule_fire_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("sessions")}
    with op.batch_alter_table("sessions") as batch_op:
        if "model_override" not in columns:
            batch_op.add_column(sa.Column("model_override", sa.String(), nullable=True))
        if "model_override_provider_id" not in columns:
            batch_op.add_column(sa.Column("model_override_provider_id", sa.String(), nullable=True))
        if "reasoning_effort_override" not in columns:
            batch_op.add_column(sa.Column("reasoning_effort_override", sa.String(), nullable=True))
        if "fast_mode_override" not in columns:
            batch_op.add_column(sa.Column("fast_mode_override", sa.Boolean(), nullable=True))
        if "runtime_override_revision" not in columns:
            batch_op.add_column(
                sa.Column(
                    "runtime_override_revision",
                    sa.BigInteger(),
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    columns = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("sessions")}
    with op.batch_alter_table("sessions") as batch_op:
        for name in (
            "runtime_override_revision",
            "fast_mode_override",
            "reasoning_effort_override",
            "model_override_provider_id",
            "model_override",
        ):
            if name in columns:
                batch_op.drop_column(name)
