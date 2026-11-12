"""Qualify bounded global Cognis/proxy session queries with a shared day-leading index."""

from alembic import op
from sqlalchemy import inspect

revision = "184_usage_session_day_index"
down_revision = "183_usage_maintenance_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bootstrap may already have installed this non-partial index."""
    if "ix_usage_session_day" not in {
        item["name"] for item in inspect(op.get_bind()).get_indexes("usage_session_totals")
    }:
        op.create_index(
            "ix_usage_session_day",
            "usage_session_totals",
            ["bucket_day", "owner_key", "generation"],
            unique=False,
        )


def downgrade() -> None:
    """Remove only the query accelerator, never retained session facts."""
    op.drop_index("ix_usage_session_day", table_name="usage_session_totals")
