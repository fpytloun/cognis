"""Whole-document human grants and recoverable owner-managed public links."""

from alembic import op

from cognis.store.document_sharing_schema import ensure_document_sharing

revision = "180_document_sharing"
down_revision = "179_merge_feedback_app_contact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_document_sharing(op.get_bind())


def downgrade() -> None:
    op.drop_column("feedback_shares", "link_mode")
    op.drop_column("feedback_shares", "token_format")
    op.drop_index("ix_document_user_grants_principal", "document_user_grants")
    op.drop_table("document_user_grants")
