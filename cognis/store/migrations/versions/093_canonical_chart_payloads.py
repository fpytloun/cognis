"""Upgrade supported inline legacy chart payloads.

Revision ID: 093_canonical_chart_payloads
Revises: 092_local_model_byte_bigint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cognis.rendering.rich_visuals import upgrade_legacy_chart_payload

revision: str = "093_canonical_chart_payloads"
down_revision: str | Sequence[str] | None = "092_local_model_byte_bigint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "deliverables" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("deliverables")}
    if not {"deliverable_id", "rich_payload"}.issubset(columns):
        return

    deliverables = sa.Table("deliverables", sa.MetaData(), autoload_with=bind)
    cursor: str | None = None
    while True:
        query = (
            sa.select(deliverables.c.deliverable_id, deliverables.c.rich_payload)
            .where(deliverables.c.rich_payload.is_not(None))
            .order_by(deliverables.c.deliverable_id)
            .limit(100)
        )
        if cursor is not None:
            query = query.where(deliverables.c.deliverable_id > cursor)
        rows = bind.execute(query).all()
        if not rows:
            return
        for deliverable_id, payload in rows:
            cursor = str(deliverable_id)
            if not isinstance(payload, dict):
                continue
            result = upgrade_legacy_chart_payload(payload)
            if result.upgraded_blocks:
                bind.execute(
                    deliverables.update()
                    .where(deliverables.c.deliverable_id == deliverable_id)
                    .values(rich_payload=result.payload)
                )


def downgrade() -> None:
    """Best-effort no-op: legacy chart rows cannot be reconstructed losslessly."""
