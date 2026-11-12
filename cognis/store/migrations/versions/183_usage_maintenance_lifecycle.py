"""Indexed usage expiry and bounded recoverable housekeeping."""

from alembic import op

from cognis.store.usage.schema import (
    ensure_usage_maintenance_lifecycle,
    rollback_usage_maintenance_lifecycle,
)

revision = "183_usage_maintenance_lifecycle"
down_revision = "182_usage_proxy_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep bootstrap and formal evolution on the same owned metadata."""
    ensure_usage_maintenance_lifecycle(op.get_bind())


def downgrade() -> None:
    """Refuse to abandon active cleanup before removing its lifecycle fields."""
    rollback_usage_maintenance_lifecycle(op.get_bind())
