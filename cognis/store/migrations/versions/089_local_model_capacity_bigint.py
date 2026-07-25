"""Widen local-model capacity assessment markers.

Revision ID: 089_local_model_capacity_bigint
Revises: 088_local_model_runtime
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "089_local_model_capacity_bigint"
down_revision: str | Sequence[str] | None = "088_local_model_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "local_model_deployments"
_COLUMN = "capacity_assessment_generation"


def _current_type() -> sa.types.TypeEngine[object] | None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return None
    for column in inspector.get_columns(_TABLE):
        if column["name"] == _COLUMN:
            return column["type"]
    return None


def upgrade() -> None:
    current_type = _current_type()
    if current_type is None or isinstance(current_type, sa.BigInteger):
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    current_type = _current_type()
    if current_type is None or not isinstance(current_type, sa.BigInteger):
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
