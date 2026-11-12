"""Add recipient-scoped app-channel delivery metadata."""

from __future__ import annotations

from alembic import op

from cognis.store.app_contact_schema import ensure_app_contact_schema

revision = "177_app_agent_contact"
down_revision = "176_task_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_app_contact_schema(op.get_bind())


def downgrade() -> None:
    op.drop_table("app_contact_blocks")
    op.drop_table("app_channel_deliveries")
