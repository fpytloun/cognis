"""Default omitted task delivery to the preferred channel."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cognis.store.migrations.versioning import ensure_alembic_version_capacity

revision: str = "126_task_delivery_preferred_channel_default"
down_revision: str | Sequence[str] | None = "125_task_source_session"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Schema-baseline adoption can stamp revision 125 without replaying the
    # earlier migration that widened Alembic's historical VARCHAR(32) column.
    # Expand it before Alembic writes this migration's longer revision ID.
    ensure_alembic_version_capacity(op.get_bind())

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.alter_column(
            "delivery_mode",
            existing_type=sa.String(),
            existing_server_default="same_conversation",
            server_default="preferred_channel",
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.alter_column(
            "delivery_mode",
            existing_type=sa.String(),
            existing_server_default="preferred_channel",
            server_default="same_conversation",
        )
