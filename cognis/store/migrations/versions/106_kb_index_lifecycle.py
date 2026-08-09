"""Add generation-fenced knowledgebase indexing lifecycle."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "106_kb_index_lifecycle"
down_revision: str | Sequence[str] | None = "105_managed_join_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> dict[str, dict[str, object]]:
    return {
        str(column["name"]): column for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _indexes(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade() -> None:
    attachment_columns = _columns("knowledgebase_artifacts")
    for column in (
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("pending_artifact_id", sa.String(), nullable=True),
        sa.Column("pending_source_hash", sa.String(), nullable=True),
        sa.Column("active_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("desired_generation", sa.Integer(), nullable=False, server_default="0"),
    ):
        if column.name not in attachment_columns:
            op.add_column("knowledgebase_artifacts", column)

    chunk_columns = _columns("knowledgebase_chunks")
    if "generation" not in chunk_columns:
        op.add_column(
            "knowledgebase_chunks",
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        )
    job_columns = _columns("knowledgebase_index_jobs")
    added_job_generation = "generation" not in job_columns
    if added_job_generation:
        op.add_column(
            "knowledgebase_index_jobs",
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        )
        op.execute(
            sa.text(
                "UPDATE knowledgebase_index_jobs "
                "SET status = 'cancelled', error = 'cancelled_by_generation_schema_upgrade', "
                "completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE status IN ('queued', 'running')"
            )
        )

    op.execute(
        sa.text(
            "UPDATE knowledgebase_artifacts SET active_generation = 1 "
            "WHERE active_generation = 0 AND (chunk_count > 0 OR EXISTS ("
            "SELECT 1 FROM knowledgebase_chunks "
            "WHERE knowledgebase_chunks.kb_artifact_id = "
            "knowledgebase_artifacts.kb_artifact_id))"
        )
    )
    op.execute(
        sa.text(
            "UPDATE knowledgebase_artifacts "
            "SET desired_generation = active_generation "
            "WHERE desired_generation < active_generation"
        )
    )
    op.execute(
        sa.text(
            "UPDATE knowledgebase_chunks SET generation = 1 "
            "WHERE generation = 0 AND EXISTS ("
            "SELECT 1 FROM knowledgebase_artifacts "
            "WHERE knowledgebase_artifacts.kb_artifact_id = "
            "knowledgebase_chunks.kb_artifact_id "
            "AND knowledgebase_artifacts.active_generation = 1)"
        )
    )
    attachment_indexes = _indexes("knowledgebase_artifacts")
    if "ix_kb_artifacts_pending_artifact" not in attachment_indexes:
        op.create_index(
            "ix_kb_artifacts_pending_artifact",
            "knowledgebase_artifacts",
            ["pending_artifact_id"],
        )
    if "uq_kb_artifacts_live_source_path" not in attachment_indexes:
        op.create_index(
            "uq_kb_artifacts_live_source_path",
            "knowledgebase_artifacts",
            ["knowledgebase_id", "source_path"],
            unique=True,
            sqlite_where=sa.text(
                "source_path IS NOT NULL AND status NOT IN ('detached', 'removed')"
            ),
            postgresql_where=sa.text(
                "source_path IS NOT NULL AND status NOT IN ('detached', 'removed')"
            ),
        )

    chunk_indexes = _indexes("knowledgebase_chunks")
    if "ix_kb_chunks_attachment_index" in chunk_indexes:
        op.drop_index("ix_kb_chunks_attachment_index", table_name="knowledgebase_chunks")
    if "ix_kb_chunks_attachment_generation_index" not in chunk_indexes:
        op.create_index(
            "ix_kb_chunks_attachment_generation_index",
            "knowledgebase_chunks",
            ["kb_artifact_id", "generation", "chunk_index"],
        )

    if "uq_kb_jobs_live_attachment_generation_type" not in _indexes("knowledgebase_index_jobs"):
        op.create_index(
            "uq_kb_jobs_live_attachment_generation_type",
            "knowledgebase_index_jobs",
            ["kb_artifact_id", "generation", "job_type"],
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'running')"),
            postgresql_where=sa.text("status IN ('queued', 'running')"),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_kb_jobs_live_attachment_generation_type",
        table_name="knowledgebase_index_jobs",
    )
    op.drop_index(
        "ix_kb_chunks_attachment_generation_index",
        table_name="knowledgebase_chunks",
    )
    op.create_index(
        "ix_kb_chunks_attachment_index",
        "knowledgebase_chunks",
        ["kb_artifact_id", "chunk_index"],
    )
    op.drop_index(
        "uq_kb_artifacts_live_source_path",
        table_name="knowledgebase_artifacts",
    )
    op.drop_index(
        "ix_kb_artifacts_pending_artifact",
        table_name="knowledgebase_artifacts",
    )
    with op.batch_alter_table("knowledgebase_index_jobs") as batch:
        batch.drop_column("generation")
    with op.batch_alter_table("knowledgebase_chunks") as batch:
        batch.drop_column("generation")
    with op.batch_alter_table("knowledgebase_artifacts") as batch:
        batch.drop_column("desired_generation")
        batch.drop_column("active_generation")
        batch.drop_column("pending_source_hash")
        batch.drop_column("pending_artifact_id")
        batch.drop_column("source_path")
