"""Canonical content-free usage registrations, prices and sparse projections."""

from __future__ import annotations

from alembic import op

from cognis.store.models import Base
from cognis.store.usage.models import USAGE_TABLE_NAMES
from cognis.store.usage.schema import ensure_usage_schema

revision = "180_usage_accounting"
down_revision = "179_merge_feedback_app_contact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add accounting metadata without changing existing model/session tables."""
    ensure_usage_schema(op.get_bind())


def downgrade() -> None:
    """Explicit offline rollback; callers must first stop newer writers."""
    connection = op.get_bind()
    for name in reversed(USAGE_TABLE_NAMES):
        Base.metadata.tables[name].drop(connection, checkfirst=True)
