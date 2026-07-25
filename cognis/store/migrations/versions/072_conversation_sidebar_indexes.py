"""Index conversations for sidebar list queries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "072_conversation_sidebar_indexes"
down_revision = "071_chat_client_transactions"
branch_labels = None
depends_on = None


def _index_exists(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == name for index in inspector.get_indexes("conversations"))


def _create_index_if_missing(name: str, columns: list[str]) -> None:
    if not _index_exists(name):
        op.create_index(name, "conversations", columns)


def _drop_index_if_exists(name: str) -> None:
    if _index_exists(name):
        op.drop_index(name, table_name="conversations")


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE conversations SET last_message_at = created_at WHERE last_message_at IS NULL"
        )
    )
    _create_index_if_missing(
        "ix_conversations_owner_activity",
        ["user_email", "status", "last_message_at", "created_at"],
    )
    _create_index_if_missing(
        "ix_conversations_owner_agent_context",
        ["user_email", "status", "agent_id", "context_type"],
    )


def downgrade() -> None:
    _drop_index_if_exists("ix_conversations_owner_agent_context")
    _drop_index_if_exists("ix_conversations_owner_activity")
