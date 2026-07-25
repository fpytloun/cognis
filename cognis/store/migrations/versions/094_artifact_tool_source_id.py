"""Add explicit lazy tool-artifact source identity.

Revision ID: 094_artifact_tool_source_id
Revises: 093_canonical_chart_payloads
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "094_artifact_tool_source_id"
down_revision: str | Sequence[str] | None = "093_canonical_chart_payloads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("artifacts")}
    if "source_tool_call_id" not in columns:
        op.add_column("artifacts", sa.Column("source_tool_call_id", sa.String(), nullable=True))
    if "source_anchor" not in columns:
        op.add_column("artifacts", sa.Column("source_anchor", sa.String(), nullable=True))
    artifacts = sa.Table("artifacts", sa.MetaData(), autoload_with=bind)
    bind.execute(
        artifacts.update()
        .where(
            artifacts.c.purpose == "tool_artifact",
            artifacts.c.source_tool_call_id.is_(None),
            artifacts.c.conversation_id.is_not(None),
        )
        .values(source_tool_call_id=artifacts.c.conversation_id)
    )
    bind.execute(
        artifacts.update()
        .where(
            artifacts.c.purpose == "tool_artifact",
            artifacts.c.source_anchor.is_(None),
            artifacts.c.session_id.is_not(None),
        )
        .values(source_anchor=artifacts.c.session_id)
    )
    legacy_tool_outputs = bind.execute(
        sa.select(artifacts.c.artifact_id, artifacts.c.filename).where(
            artifacts.c.purpose == "tool_output",
            artifacts.c.source_tool_call_id.is_(None),
            artifacts.c.filename.is_not(None),
        )
    ).all()
    for artifact_id, filename in legacy_tool_outputs:
        filename_text = str(filename or "")
        if not filename_text.endswith(".txt"):
            continue
        source_tool_call_id = filename_text[:-4].strip()
        if not source_tool_call_id:
            continue
        bind.execute(
            artifacts.update()
            .where(
                artifacts.c.artifact_id == artifact_id,
                artifacts.c.source_tool_call_id.is_(None),
            )
            .values(source_tool_call_id=source_tool_call_id)
        )
    indexes = {index["name"] for index in inspector.get_indexes("artifacts")}
    if "ix_artifacts_tool_source" not in indexes:
        op.create_index(
            "ix_artifacts_tool_source",
            "artifacts",
            ["owner_email", "source_tool_call_id", "source_anchor"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "artifacts" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("artifacts")}
    if "ix_artifacts_tool_source" in indexes:
        op.drop_index("ix_artifacts_tool_source", table_name="artifacts")
    columns = {column["name"] for column in inspector.get_columns("artifacts")}
    if "source_anchor" in columns:
        op.drop_column("artifacts", "source_anchor")
    if "source_tool_call_id" in columns:
        op.drop_column("artifacts", "source_tool_call_id")
