"""Index schedules for owner-scoped actionable issue projection.

Revision ID: 142_schedule_action_issue_index
Revises: 141_work_overview_evidence_index
"""

import sqlalchemy as sa
from alembic import op

revision = "142_schedule_action_issue_index"
down_revision = "141_work_overview_evidence_index"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_schedules_owner_run_status_updated"
RECONCILE_INDEX_NAME = "ix_schedules_run_status_updated"


def upgrade() -> None:
    indexes = {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes("schedules")
        if index.get("name")
    }
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "schedules",
            ["created_by", "last_run_status", "updated_at"],
        )
    if RECONCILE_INDEX_NAME not in indexes:
        op.create_index(
            RECONCILE_INDEX_NAME,
            "schedules",
            ["last_run_status", "updated_at"],
        )


def downgrade() -> None:
    op.drop_index(RECONCILE_INDEX_NAME, table_name="schedules")
    op.drop_index(INDEX_NAME, table_name="schedules")
