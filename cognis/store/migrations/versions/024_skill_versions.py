"""Add skill_versions and skill_assets tables for versioned skill support.

Adds immutable skill version records and skill asset linkage.
Updates skills table with current_version_id reference.

Revision ID: 024_skill_versions
Revises: 023_executor_runtime_state
Create Date: 2026-04-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024_skill_versions"
down_revision = "023_executor_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- skill_versions: immutable version records ---
    op.create_table(
        "skill_versions",
        sa.Column("version_id", sa.String(), primary_key=True),
        sa.Column(
            "skill_id",
            sa.String(),
            sa.ForeignKey("skills.skill_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=True),
        sa.Column("prompt_templates", sa.JSON(), nullable=True),
        sa.Column("secret_placeholders", sa.JSON(), nullable=True),
        # Import provenance
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("resolved_url", sa.String(), nullable=True),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("import_checksum", sa.String(), nullable=True),
        sa.Column("imported_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("import_format", sa.String(), nullable=True),
        # Asset manifest (list of {filename, asset_id, content_hash, size_bytes, content_type})
        sa.Column("asset_manifest", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("skill_id", "version_number", name="uq_skill_version_number"),
    )
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])

    # --- skill_assets: per-version asset records linked to artifact store ---
    op.create_table(
        "skill_assets",
        sa.Column("asset_id", sa.String(), primary_key=True),
        sa.Column(
            "skill_version_id",
            sa.String(),
            sa.ForeignKey("skill_versions.version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("artifact_namespace", sa.String(), nullable=False, server_default="skills"),
        sa.Column("artifact_object_id", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "content_type", sa.String(), nullable=False, server_default="application/octet-stream"
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_skill_assets_version_id", "skill_assets", ["skill_version_id"])

    # --- Update skills table: add current_version_id ---
    op.add_column(
        "skills",
        sa.Column("current_version_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skills", "current_version_id")
    op.drop_table("skill_assets")
    op.drop_table("skill_versions")
