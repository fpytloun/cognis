"""Add task and schedule finish instructions."""

from __future__ import annotations

from alembic import op
from sqlalchemy import delete, inspect

from cognis.store.app_channels import ensure_app_channels
from cognis.store.models import ChannelAccountRow
from cognis.store.task_outcome_schema import ensure_task_outcome_schema

revision = "176_task_outcomes"
down_revision = "175_audio_length_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_task_outcome_schema(op.get_bind())
    ensure_app_channels(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    if "channel_accounts" in inspector.get_table_names():
        connection.execute(delete(ChannelAccountRow).where(ChannelAccountRow.channel_type == "web"))
    for table in ("tasks", "schedules"):
        if table in inspector.get_table_names() and "on_finish_instructions" in {
            column["name"] for column in inspector.get_columns(table)
        }:
            op.drop_column(table, "on_finish_instructions")
