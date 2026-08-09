"""Add generic operational activity scopes to sessions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "124_session_activity_scope"
down_revision: str | Sequence[str] | None = "123_direct_turn_retry_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_activity_scopes(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            """
            SELECT session_id, parent_session_id, previous_session_id, completion_reason
            FROM sessions
            ORDER BY started_at, session_id
            """
        )
    ).mappings()
    by_id = {str(row["session_id"]): dict(row) for row in rows}
    scopes: dict[str, str] = {}
    unresolved = set(by_id)
    while unresolved:
        progressed = False
        for session_id in sorted(unresolved):
            row = by_id[session_id]
            parent_id = row["parent_session_id"]
            previous_id = row["previous_session_id"]
            if parent_id:
                if parent_id in by_id and parent_id not in scopes:
                    continue
                scopes[session_id] = scopes.get(parent_id, session_id)
            elif previous_id:
                if previous_id in by_id and previous_id not in scopes:
                    continue
                predecessor = by_id.get(previous_id)
                scopes[session_id] = (
                    session_id
                    if predecessor and predecessor["completion_reason"] == "user_reset"
                    else scopes.get(previous_id, session_id)
                )
            else:
                scopes[session_id] = session_id
            unresolved.remove(session_id)
            progressed = True
        if not progressed:
            for session_id in unresolved:
                scopes[session_id] = session_id
            break
    for session_id, activity_scope_id in scopes.items():
        connection.execute(
            sa.text(
                "UPDATE sessions SET activity_scope_id = :activity_scope_id "
                "WHERE session_id = :session_id"
            ),
            {"session_id": session_id, "activity_scope_id": activity_scope_id},
        )


def upgrade() -> None:
    op.add_column("sessions", sa.Column("activity_scope_id", sa.String(), nullable=True))
    _backfill_activity_scopes(op.get_bind())
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column(
            "activity_scope_id",
            existing_type=sa.String(),
            nullable=False,
        )


def downgrade() -> None:
    op.drop_column("sessions", "activity_scope_id")
