"""Persist source-aware relative podcast length plans."""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from cognis.store.audio_schema import ensure_audio_job_plans

revision = "175_audio_length_plans"
down_revision = "174_native_binary_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable fields without changing legacy job budgets or recordings."""
    ensure_audio_job_plans(op.get_bind())


def downgrade() -> None:
    """Remove planning metadata without removing audio jobs or their artifacts."""
    connection = op.get_bind()
    inspector = inspect(connection)
    if "audio_jobs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("audio_jobs")}
    for name in ("target_words", "length_preference"):
        if name in columns:
            op.drop_column("audio_jobs", name)
