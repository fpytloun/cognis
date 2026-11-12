"""Remove private objectives from generated recipient chat titles."""

from __future__ import annotations

from alembic import op

from cognis.store.app_contact_schema import repair_app_contact_titles

revision = "178_app_contact_titles"
down_revision = "177_app_agent_contact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    repair_app_contact_titles(op.get_bind())


def downgrade() -> None:
    """Keep public titles; downgrading must not restore private instructions."""
