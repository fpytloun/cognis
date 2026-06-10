"""add conversation title source

Revision ID: 029_conversation_title_source
Revises: 028_credentials_table
Create Date: 2026-04-12 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "029_conversation_title_source"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("title_source", sa.String(), nullable=False, server_default="unset"),
    )
    op.execute(
        "UPDATE conversations SET title_source = CASE "
        "WHEN title IS NULL OR TRIM(title) = '' THEN 'unset' "
        "ELSE 'manual' END"
    )
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "title_source",
            existing_type=sa.String(),
            server_default=None,
        )


def downgrade() -> None:
    op.drop_column("conversations", "title_source")
