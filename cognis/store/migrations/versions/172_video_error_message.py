"""Add one nullable, opt-in redacted provider diagnostic field."""

from alembic import op
from sqlalchemy import inspect

from cognis.store.video_schema import ensure_video_error_message

revision = "172_video_error_message"
down_revision = "171_audio_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ensure_video_error_message(op.get_bind())


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "video_jobs" in inspector.get_table_names() and "provider_error_message" in {
        column["name"] for column in inspector.get_columns("video_jobs")
    }:
        op.drop_column("video_jobs", "provider_error_message")
