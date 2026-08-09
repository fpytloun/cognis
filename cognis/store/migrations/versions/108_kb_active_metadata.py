"""Add generation-consistent active knowledgebase metadata."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "108_kb_active_metadata"
down_revision: str | Sequence[str] | None = "107_knowledgebase_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("knowledgebase_artifacts")
    }
    if "active_metadata" not in columns:
        op.add_column(
            "knowledgebase_artifacts",
            sa.Column("active_metadata", sa.JSON(), nullable=True),
        )
    op.execute(
        sa.text(
            "UPDATE knowledgebase_artifacts SET active_metadata = metadata "
            "WHERE active_generation > 0 AND active_metadata IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("knowledgebase_artifacts", "active_metadata")
