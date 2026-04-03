"""Add artifact records table for multimodal attachments."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021_artifact_records"
down_revision = "020_agent_avatar_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("owner_email", sa.String(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("message_role", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False, server_default="chat_input"),
        sa.Column("kind", sa.String(), nullable=False, server_default="file"),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="temporary"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index("ix_artifacts_owner_status", "artifacts", ["owner_email", "status"])
    op.create_index("ix_artifacts_conversation", "artifacts", ["conversation_id"])
    op.create_index("ix_artifacts_expiry", "artifacts", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_expiry", table_name="artifacts")
    op.drop_index("ix_artifacts_conversation", table_name="artifacts")
    op.drop_index("ix_artifacts_owner_status", table_name="artifacts")
    op.drop_table("artifacts")
