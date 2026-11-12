"""Persist audio voice-fallback warnings."""

from alembic import op
from sqlalchemy import inspect

from cognis.store.audio_schema import ensure_audio_job_warnings

revision = "173_audio_voice_warnings"
down_revision = "172_video_error_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_audio_job_warnings(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    if "audio_jobs" in inspector.get_table_names() and "warnings" in {
        column["name"] for column in inspector.get_columns("audio_jobs")
    }:
        op.drop_column("audio_jobs", "warnings")
