"""Add durable managed-channel resume correlation.

Revision ID: 115_managed_channel_resume
Revises: 114_managed_channel_fences
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "115_managed_channel_resume"
down_revision: str | Sequence[str] | None = "114_managed_channel_fences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _named_unique_representation(
    inspector: sa.Inspector,
    *,
    table_name: str,
    name: str,
    expected_columns: Sequence[str],
) -> str | None:
    expected = set(expected_columns)
    definitions: list[tuple[str, bool, tuple[str, ...]]] = []
    for constraint in inspector.get_unique_constraints(table_name):
        if constraint.get("name") == name:
            columns = tuple(constraint.get("column_names") or ())
            definitions.append(("constraint", True, columns))
    for index in inspector.get_indexes(table_name):
        if index.get("name") == name:
            columns = tuple(index.get("column_names") or ())
            definitions.append(("index", bool(index.get("unique")), columns))
    if not definitions:
        return None
    incompatible = [
        (kind, unique, columns)
        for kind, unique, columns in definitions
        if not unique or len(columns) != len(expected_columns) or set(columns) != expected
    ]
    if incompatible:
        raise RuntimeError(
            f"{table_name}.{name} exists with an incompatible unique definition: "
            f"{incompatible!r}; expected unique columns {tuple(expected_columns)!r}"
        )
    return (
        "constraint"
        if any(kind == "constraint" for kind, _unique, _columns in definitions)
        else "index"
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("managed_conversation_signals")}
    resume_unique = _named_unique_representation(
        inspector,
        table_name="managed_conversation_signals",
        name="uq_managed_signal_resume_request",
        expected_columns=("resume_request_id",),
    )
    with op.batch_alter_table("managed_conversation_signals") as batch:
        if "resume_request_id" not in columns:
            batch.add_column(sa.Column("resume_request_id", sa.String(), nullable=True))
        if "resume_turn_id" not in columns:
            batch.add_column(sa.Column("resume_turn_id", sa.String(), nullable=True))
        if "resume_admitted_at" not in columns:
            batch.add_column(
                sa.Column("resume_admitted_at", sa.TIMESTAMP(timezone=True), nullable=True)
            )
        if "resume_terminal_status" not in columns:
            batch.add_column(sa.Column("resume_terminal_status", sa.String(), nullable=True))
        if resume_unique is None:
            batch.create_unique_constraint(
                "uq_managed_signal_resume_request", ["resume_request_id"]
            )


def downgrade() -> None:
    resume_unique = _named_unique_representation(
        sa.inspect(op.get_bind()),
        table_name="managed_conversation_signals",
        name="uq_managed_signal_resume_request",
        expected_columns=("resume_request_id",),
    )
    if resume_unique is None:
        raise RuntimeError(
            "managed_conversation_signals.uq_managed_signal_resume_request "
            "is missing during downgrade"
        )
    with op.batch_alter_table("managed_conversation_signals") as batch:
        if resume_unique == "constraint":
            batch.drop_constraint("uq_managed_signal_resume_request", type_="unique")
        else:
            batch.drop_index("uq_managed_signal_resume_request")
        batch.drop_column("resume_terminal_status")
        batch.drop_column("resume_admitted_at")
        batch.drop_column("resume_turn_id")
        batch.drop_column("resume_request_id")
