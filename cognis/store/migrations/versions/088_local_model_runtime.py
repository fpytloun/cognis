"""Add managed local-model runtime operation intent.

Revision ID: 088_local_model_runtime
Revises: 087_local_model_foundation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "088_local_model_runtime"
down_revision: str | Sequence[str] | None = "087_local_model_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("local_model_operations")}
    if "post_pull_provider_upsert" not in columns:
        op.add_column(
            "local_model_operations",
            sa.Column(
                "post_pull_provider_upsert",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("local_model_operations")}
    if "post_pull_provider_upsert" in columns:
        op.drop_column("local_model_operations", "post_pull_provider_upsert")
