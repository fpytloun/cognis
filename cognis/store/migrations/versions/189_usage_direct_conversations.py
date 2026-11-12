"""Retain sessionless conversation Usage contributions."""

from alembic import op

from cognis.store.usage.schema import (
    ensure_usage_direct_sessions,
    rollback_usage_direct_sessions,
)

revision = "189_usage_direct_conversations"
down_revision = "188_agent_skill_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_usage_direct_sessions(op.get_bind())


def downgrade() -> None:
    rollback_usage_direct_sessions(op.get_bind())
