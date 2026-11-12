"""Add revision-pinned deliverable audio jobs.

Revision ID: 171_audio_jobs
Revises: 170_palette_destinations
"""

import sqlalchemy as sa
from alembic import op

revision = "171_audio_jobs"
down_revision = "170_palette_destinations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Match the idempotent new-table startup path in Base.metadata.create_all."""
    if sa.inspect(op.get_bind()).has_table("audio_jobs"):
        return
    op.create_table(
        "audio_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("owner_email", sa.String(), nullable=False),
        sa.Column(
            "deliverable_id",
            sa.String(),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("target_minutes", sa.Integer(), nullable=False),
        sa.Column("voice", sa.String(255)),
        sa.Column("model", sa.String(255)),
        sa.Column("provider_id", sa.String(128)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("script_artifact_id", sa.String(64)),
        sa.Column("part_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("total_parts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_email", "idempotency_key", name="uq_audio_job_request"),
    )
    op.create_index("ix_audio_job_queue", "audio_jobs", ["status", "created_at"])
    op.create_index(
        "ix_audio_job_source", "audio_jobs", ["owner_email", "deliverable_id", "created_at"]
    )


def downgrade() -> None:
    """Remove only this revision's table."""
    if sa.inspect(op.get_bind()).has_table("audio_jobs"):
        op.drop_table("audio_jobs")
