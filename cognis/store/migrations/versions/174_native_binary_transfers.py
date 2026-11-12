"""Call-scoped native-binary upload grants and publication receipts."""

from alembic import op
from sqlalchemy import text

from cognis.store.binary_transfer_schema import ensure_binary_transfer_schema

revision = "174_native_binary_transfers"
down_revision = "173_audio_voice_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create metadata tables without storing any file bodies in the database."""
    ensure_binary_transfer_schema(op.get_bind())


def downgrade() -> None:
    """Remove transfer metadata without truncating existing artifact lengths."""
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        large = connection.scalar(
            text("SELECT 1 FROM artifacts WHERE size_bytes > 2147483647 LIMIT 1")
        )
        if large:
            raise RuntimeError("Cannot downgrade artifact sizes while files exceed 2 GiB.")
        connection.execute(text("ALTER TABLE artifacts ALTER COLUMN size_bytes TYPE INTEGER"))
    op.drop_table("binary_transfers")
    op.drop_table("binary_transfer_scopes")
