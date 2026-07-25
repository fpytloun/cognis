"""Repair legacy inline deliverable content nullability.

Revision ID: 076_repair_legacy_deliverable_content_nullable
Revises: 075_deliverable_object_store_payloads
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "076_repair_legacy_deliverable_content_nullable"
down_revision = "075_deliverable_object_store_payloads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("deliverables")
    }
    content_column = columns.get("content")
    if content_column is not None and content_column.get("nullable") is False:
        with op.batch_alter_table("deliverables") as batch:
            batch.alter_column("content", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # Object-store deliverables legitimately leave this obsolete column NULL.
    # Restoring NOT NULL would break rows written after this repair.
    pass
