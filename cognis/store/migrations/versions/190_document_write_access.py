"""Lineage write grants and revision authors."""

from alembic import op

from cognis.store.document_write_schema import (
    ensure_document_write_schema,
    rollback_document_write_schema,
)

revision = "190_document_write_access"
down_revision = "189_usage_direct_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_document_write_schema(op.get_bind())


def downgrade() -> None:
    rollback_document_write_schema(op.get_bind())
