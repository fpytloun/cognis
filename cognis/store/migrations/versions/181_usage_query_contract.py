"""Global query paths, typed model names and canonical backfill bindings."""

from alembic import op

from cognis.store.usage.schema import ensure_usage_schema, rollback_usage_query_contract

revision = "181_usage_query_contract"
down_revision = "180_usage_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bootstrap shares the same idempotent table/index creation owner."""
    ensure_usage_schema(op.get_bind())


def downgrade() -> None:
    """Stop accounting workers before explicitly removing newer metadata."""
    rollback_usage_query_contract(op.get_bind())
