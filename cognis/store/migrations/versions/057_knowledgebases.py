"""Add artifact-backed knowledgebase tables.

Revision ID: 057_knowledgebases
Revises: 056_llm_provider_owner
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "057_knowledgebases"
down_revision = "056_llm_provider_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "knowledgebases",
        sa.Column("knowledgebase_id", sa.String(), primary_key=True),
        sa.Column("owner_email", sa.String(), sa.ForeignKey("users.email"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("metadata_schema", sa.JSON(), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_knowledgebases_owner_status", "knowledgebases", ["owner_email", "status"])
    op.create_index("ix_knowledgebases_owner_name", "knowledgebases", ["owner_email", "name"])

    op.create_table(
        "knowledgebase_artifacts",
        sa.Column("kb_artifact_id", sa.String(), primary_key=True),
        sa.Column(
            "knowledgebase_id",
            sa.String(),
            sa.ForeignKey("knowledgebases.knowledgebase_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=True),
        sa.Column("source_size_bytes", sa.Integer(), nullable=True),
        sa.Column("source_mime_type", sa.String(), nullable=True),
        sa.Column("source_filename", sa.String(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=True),
        sa.Column("embedding_provider_id", sa.String(), nullable=True),
        sa.Column("vector_dimension", sa.Integer(), nullable=True),
        sa.Column("last_job_id", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_diagnostics", sa.JSON(), nullable=True),
        sa.Column("attached_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("stale_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("removed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_kb_artifacts_kb_status", "knowledgebase_artifacts", ["knowledgebase_id", "status"]
    )
    op.create_index("ix_kb_artifacts_artifact", "knowledgebase_artifacts", ["artifact_id"])

    op.create_table(
        "knowledgebase_chunks",
        sa.Column("chunk_id", sa.String(), primary_key=True),
        sa.Column("knowledgebase_id", sa.String(), nullable=False),
        sa.Column("kb_artifact_id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("artifact_hash", sa.String(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("vector_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_kb_chunks_kb_artifact", "knowledgebase_chunks", ["knowledgebase_id", "artifact_id"]
    )
    op.create_index(
        "ix_kb_chunks_attachment_index", "knowledgebase_chunks", ["kb_artifact_id", "chunk_index"]
    )

    op.create_table(
        "knowledgebase_index_jobs",
        sa.Column("job_id", sa.String(), primary_key=True),
        sa.Column("knowledgebase_id", sa.String(), nullable=False),
        sa.Column("kb_artifact_id", sa.String(), nullable=True),
        sa.Column("artifact_id", sa.String(), nullable=True),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("diagnostics", sa.JSON(), nullable=True),
        sa.Column("chunks_indexed", sa.Integer(), nullable=False),
        sa.Column("chunks_deleted", sa.Integer(), nullable=False),
        sa.Column("queued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_kb_jobs_status_queue",
        "knowledgebase_index_jobs",
        ["status", "priority", "queued_at"],
    )
    op.create_index(
        "ix_kb_jobs_kb_status", "knowledgebase_index_jobs", ["knowledgebase_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_kb_jobs_kb_status", table_name="knowledgebase_index_jobs")
    op.drop_index("ix_kb_jobs_status_queue", table_name="knowledgebase_index_jobs")
    op.drop_table("knowledgebase_index_jobs")
    op.drop_index("ix_kb_chunks_attachment_index", table_name="knowledgebase_chunks")
    op.drop_index("ix_kb_chunks_kb_artifact", table_name="knowledgebase_chunks")
    op.drop_table("knowledgebase_chunks")
    op.drop_index("ix_kb_artifacts_artifact", table_name="knowledgebase_artifacts")
    op.drop_index("ix_kb_artifacts_kb_status", table_name="knowledgebase_artifacts")
    op.drop_table("knowledgebase_artifacts")
    op.drop_index("ix_knowledgebases_owner_name", table_name="knowledgebases")
    op.drop_index("ix_knowledgebases_owner_status", table_name="knowledgebases")
    op.drop_table("knowledgebases")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("content_hash")
