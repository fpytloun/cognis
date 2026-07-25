"""Widen persisted local-model byte counters.

Revision ID: 092_local_model_byte_bigint
Revises: 091_channel_default_profile
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "092_local_model_byte_bigint"
down_revision: str | Sequence[str] | None = "091_channel_default_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INT32_MAX = 2**31 - 1
_COLUMNS = (
    ("local_model_operations", "progress_bytes", False, "0"),
    ("local_model_target_statuses", "observed_size_bytes", True, None),
)


def _column_type(table_name: str, column_name: str) -> sa.types.TypeEngine[Any] | None:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return None
    for column in inspector.get_columns(table_name):
        if column["name"] == column_name:
            return column["type"]
    return None


def _alter(
    table_name: str,
    column_name: str,
    *,
    current_type: sa.types.TypeEngine[Any],
    target_type: sa.types.TypeEngine[Any],
    nullable: bool,
    server_default: str | None,
) -> None:
    existing_server_default = sa.text(server_default) if server_default is not None else None
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=current_type,
                type_=target_type,
                existing_nullable=nullable,
                existing_server_default=existing_server_default,
            )
        return
    op.alter_column(
        table_name,
        column_name,
        existing_type=current_type,
        type_=target_type,
        existing_nullable=nullable,
        existing_server_default=existing_server_default,
    )


def upgrade() -> None:
    for table_name, column_name, nullable, server_default in _COLUMNS:
        current_type = _column_type(table_name, column_name)
        if current_type is None or isinstance(current_type, sa.BigInteger):
            continue
        if not isinstance(current_type, sa.Integer):
            raise RuntimeError(
                f"refusing to widen {table_name}.{column_name}: unexpected type {current_type}"
            )
        _alter(
            table_name,
            column_name,
            current_type=current_type,
            target_type=sa.BigInteger(),
            nullable=nullable,
            server_default=server_default,
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns_to_narrow: list[tuple[str, str, bool, str | None, sa.types.TypeEngine[Any]]] = []
    for table_name, column_name, nullable, server_default in _COLUMNS:
        current_type = _column_type(table_name, column_name)
        if current_type is None or not isinstance(current_type, sa.BigInteger):
            continue
        oversized = bind.execute(
            sa.text(f"SELECT 1 FROM {table_name} WHERE {column_name} > :int32_max LIMIT 1"),
            {"int32_max": _INT32_MAX},
        ).first()
        if oversized is not None:
            raise RuntimeError(
                f"refusing to narrow {table_name}.{column_name} to INTEGER: "
                "stored values exceed signed int32 maximum"
            )
        columns_to_narrow.append((table_name, column_name, nullable, server_default, current_type))

    for table_name, column_name, nullable, server_default, current_type in columns_to_narrow:
        _alter(
            table_name,
            column_name,
            current_type=current_type,
            target_type=sa.Integer(),
            nullable=nullable,
            server_default=server_default,
        )
