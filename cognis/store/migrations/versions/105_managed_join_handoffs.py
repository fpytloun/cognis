"""Add durable joined managed-conversation handoff state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "105_managed_join_handoffs"
down_revision: str | Sequence[str] | None = "104_channel_direct_turn_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in (
        "handoff_state",
        "handoff_target_turn_id",
        "handoff_controller_session_id",
        "handoff_controller_turn_id",
        "handoff_tool_call_id",
    ):
        op.add_column(
            "managed_conversation_links",
            sa.Column(column_name, sa.String(), nullable=True),
        )
    op.create_index(
        "ix_managed_conversation_links_handoff_owner",
        "managed_conversation_links",
        [
            "handoff_state",
            "handoff_controller_session_id",
            "handoff_controller_turn_id",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_managed_conversation_links_handoff_owner",
        table_name="managed_conversation_links",
    )
    for column_name in (
        "handoff_tool_call_id",
        "handoff_controller_turn_id",
        "handoff_controller_session_id",
        "handoff_target_turn_id",
        "handoff_state",
    ):
        op.drop_column("managed_conversation_links", column_name)
