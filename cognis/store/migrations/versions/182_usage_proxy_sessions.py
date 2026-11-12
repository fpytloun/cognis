"""Retained proxy session correlations, separate from canonical usage dimensions."""

from alembic import op

from cognis.store.usage.schema import ensure_usage_proxy_sessions, rollback_usage_proxy_sessions

revision = "182_usage_proxy_sessions"
down_revision = "181_usage_query_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bootstrap and migration share the same idempotent owned metadata evolution."""
    ensure_usage_proxy_sessions(op.get_bind())


def downgrade() -> None:
    """Refuse to discard retained proxy correlations."""
    rollback_usage_proxy_sessions(op.get_bind())
