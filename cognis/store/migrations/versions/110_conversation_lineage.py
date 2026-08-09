"""Add typed indexed conversation lineage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "110_conversation_lineage"
down_revision: str | Sequence[str] | None = "109_task_control_conversation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "lineage_kind",
    "fork_source_conversation_id",
    "fork_source_session_id",
    "lineage_task_id",
    "lineage_step_run_id",
)
_INDEXES = (
    (
        "ix_conversations_owner_fork_conversation",
        ["user_email", "fork_source_conversation_id", "conversation_id"],
    ),
    (
        "ix_conversations_owner_fork_session",
        ["user_email", "fork_source_session_id", "conversation_id"],
    ),
    ("ix_conversations_owner_lineage_task", ["user_email", "lineage_task_id", "conversation_id"]),
    (
        "ix_conversations_owner_lineage_step",
        ["user_email", "lineage_step_run_id", "conversation_id"],
    ),
    ("ix_tasks_owner_source_ref", ["created_by", "source_ref", "task_id"]),
)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _corroborated_values(
    bind: sa.Connection,
    *,
    conversation_id: str,
    context_data: dict[str, object],
) -> dict[str, str | None]:
    child = (
        bind.execute(
            sa.text(
                """
            SELECT c.user_email, s.parent_session_id, s.previous_session_id,
                   s.source_session_id
            FROM conversations c
            LEFT JOIN sessions s ON s.session_id = c.active_session_id
            WHERE c.conversation_id = :conversation_id
            """
            ),
            {"conversation_id": conversation_id},
        )
        .mappings()
        .first()
    )
    if child is None:
        return {}
    kind = context_data.get("forked_from")
    source_session_id = _string(
        context_data.get("forked_from_session_id")
        if kind == "conversation"
        else context_data.get("source_session_id")
    )
    if not source_session_id or source_session_id not in {
        child["parent_session_id"],
        child["previous_session_id"],
        child["source_session_id"],
    }:
        return {}
    if kind == "conversation":
        source_conversation_id = _string(context_data.get("forked_from_conversation_id"))
        corroborated = bind.execute(
            sa.text(
                """
                SELECT 1 FROM sessions s
                JOIN conversations c ON c.conversation_id = s.conversation_id
                WHERE s.session_id = :session_id
                  AND c.conversation_id = :conversation_id
                  AND s.user_email = :owner AND c.user_email = :owner
                """
            ),
            {
                "session_id": source_session_id,
                "conversation_id": source_conversation_id,
                "owner": child["user_email"],
            },
        ).first()
        return (
            {
                "lineage_kind": "conversation",
                "fork_source_conversation_id": source_conversation_id,
                "fork_source_session_id": source_session_id,
            }
            if corroborated
            else {}
        )
    if kind not in {"task", "task_step"}:
        return {}
    task_id = _string(context_data.get("task_id"))
    step_id = _string(context_data.get("source_step_run_id" if kind == "task" else "step_run_id"))
    corroborated = bind.execute(
        sa.text(
            """
            SELECT 1 FROM tasks t
            JOIN step_runs sr ON sr.task_id = t.task_id
            JOIN sessions s ON s.session_id = sr.session_id
            WHERE t.task_id = :task_id AND sr.step_run_id = :step_id
              AND s.session_id = :session_id
              AND t.created_by = :owner AND s.user_email = :owner
            """
        ),
        {
            "task_id": task_id,
            "step_id": step_id,
            "session_id": source_session_id,
            "owner": child["user_email"],
        },
    ).first()
    return (
        {
            "lineage_kind": str(kind),
            "lineage_task_id": task_id,
            "lineage_step_run_id": step_id,
            "fork_source_session_id": source_session_id,
        }
        if corroborated
        else {}
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {str(column["name"]) for column in inspector.get_columns("conversations")}
    for name in _COLUMNS:
        if name not in columns:
            op.add_column("conversations", sa.Column(name, sa.String(), nullable=True))
    session_columns = {str(column["name"]) for column in inspector.get_columns("sessions")}
    if "source_session_id" not in session_columns:
        op.add_column("sessions", sa.Column("source_session_id", sa.String(), nullable=True))

    conversations = sa.table(
        "conversations",
        sa.column("conversation_id", sa.String()),
        sa.column("context_data", sa.JSON()),
        *[sa.column(name, sa.String()) for name in _COLUMNS],
    )
    rows = bind.execute(
        sa.select(conversations.c.conversation_id, conversations.c.context_data)
    ).all()
    for conversation_id, context_data in rows:
        if not isinstance(context_data, dict):
            continue
        values = _corroborated_values(
            bind,
            conversation_id=conversation_id,
            context_data=context_data,
        )
        if values:
            bind.execute(
                sa.update(conversations)
                .where(conversations.c.conversation_id == conversation_id)
                .values(**values)
            )

    for name, fields in _INDEXES:
        table = "tasks" if name == "ix_tasks_owner_source_ref" else "conversations"
        indexes = {
            str(index["name"]) for index in sa.inspect(bind).get_indexes(table) if index.get("name")
        }
        if name not in indexes:
            op.create_index(name, table, fields, unique=False)
    session_indexes = {
        str(index["name"])
        for index in sa.inspect(bind).get_indexes("sessions")
        if index.get("name")
    }
    if "ix_sessions_owner_source_session" not in session_indexes:
        op.create_index(
            "ix_sessions_owner_source_session",
            "sessions",
            ["user_email", "source_session_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_sessions_owner_source_session", table_name="sessions")
    op.drop_column("sessions", "source_session_id")
    for name, _fields in reversed(_INDEXES):
        table = "tasks" if name == "ix_tasks_owner_source_ref" else "conversations"
        op.drop_index(name, table_name=table)
    for name in reversed(_COLUMNS):
        op.drop_column("conversations", name)
