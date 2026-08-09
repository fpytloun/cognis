"""Add prepared-at fencing for managed channel resume admission.

Revision ID: 116_managed_resume_prepared
Revises: 115_managed_channel_resume
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "116_managed_resume_prepared"
down_revision = "115_managed_channel_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("managed_conversation_signals")
    }
    if "resume_prepared_at" not in columns:
        op.add_column(
            "managed_conversation_signals",
            sa.Column("resume_prepared_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("managed_conversation_signals", "resume_prepared_at")
