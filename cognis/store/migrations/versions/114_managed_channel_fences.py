"""Add managed-channel send leases and signal correlation.

Revision ID: 114_managed_channel_fences
Revises: 113_managed_channel_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "114_managed_channel_fences"
down_revision: str | Sequence[str] | None = "113_managed_channel_lifecycle"
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
    signal_columns = {
        column["name"] for column in inspector.get_columns("managed_conversation_signals")
    }
    source_turn_unique = _named_unique_representation(
        inspector,
        table_name="managed_conversation_signals",
        name="uq_managed_signal_source_turn",
        expected_columns=("link_id", "owner_epoch", "source_turn_id", "kind"),
    )
    with op.batch_alter_table("managed_conversation_signals") as batch:
        if "source_turn_id" not in signal_columns:
            batch.add_column(sa.Column("source_turn_id", sa.String(), nullable=True))
        if source_turn_unique is None:
            batch.create_unique_constraint(
                "uq_managed_signal_source_turn",
                ["link_id", "owner_epoch", "source_turn_id", "kind"],
            )
    binding_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("managed_channel_bindings")
    }
    with op.batch_alter_table("managed_channel_bindings") as batch:
        if "delivery_lease_token" not in binding_columns:
            batch.add_column(sa.Column("delivery_lease_token", sa.String(), nullable=True))
        if "delivery_lease_version" not in binding_columns:
            batch.add_column(sa.Column("delivery_lease_version", sa.BigInteger(), nullable=True))
        if "delivery_lease_owner_epoch" not in binding_columns:
            batch.add_column(
                sa.Column("delivery_lease_owner_epoch", sa.BigInteger(), nullable=True)
            )
        if "delivery_lease_expires_at" not in binding_columns:
            batch.add_column(
                sa.Column(
                    "delivery_lease_expires_at",
                    sa.DateTime(timezone=True),
                    nullable=True,
                )
            )


def downgrade() -> None:
    source_turn_unique = _named_unique_representation(
        sa.inspect(op.get_bind()),
        table_name="managed_conversation_signals",
        name="uq_managed_signal_source_turn",
        expected_columns=("link_id", "owner_epoch", "source_turn_id", "kind"),
    )
    if source_turn_unique is None:
        raise RuntimeError(
            "managed_conversation_signals.uq_managed_signal_source_turn is missing during downgrade"
        )
    with op.batch_alter_table("managed_channel_bindings") as batch:
        batch.drop_column("delivery_lease_expires_at")
        batch.drop_column("delivery_lease_owner_epoch")
        batch.drop_column("delivery_lease_version")
        batch.drop_column("delivery_lease_token")
    with op.batch_alter_table("managed_conversation_signals") as batch:
        if source_turn_unique == "constraint":
            batch.drop_constraint("uq_managed_signal_source_turn", type_="unique")
        else:
            batch.drop_index("uq_managed_signal_source_turn")
        batch.drop_column("source_turn_id")
