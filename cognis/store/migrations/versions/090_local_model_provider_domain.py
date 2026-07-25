"""Add idempotent managed-local provider identity.

Revision ID: 090_local_model_provider_domain
Revises: 089_local_model_capacity_bigint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "090_local_model_provider_domain"
down_revision: str | Sequence[str] | None = "089_local_model_capacity_bigint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("llm_providers")}
    if "managed_local_key" not in columns:
        op.add_column(
            "llm_providers",
            sa.Column("managed_local_key", sa.String(), nullable=True),
        )
    indexes = {index["name"] for index in inspector.get_indexes("llm_providers")}
    unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("llm_providers")
    }
    if "uq_llm_providers_managed_local_key" not in indexes | unique_constraints:
        op.create_index(
            "uq_llm_providers_managed_local_key",
            "llm_providers",
            ["managed_local_key"],
            unique=True,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("llm_providers")}
    if "uq_llm_providers_managed_local_key" in indexes:
        op.drop_index(
            "uq_llm_providers_managed_local_key",
            table_name="llm_providers",
        )
    columns = {column["name"] for column in inspector.get_columns("llm_providers")}
    if "managed_local_key" in columns:
        op.drop_column("llm_providers", "managed_local_key")
