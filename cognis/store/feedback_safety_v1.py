"""Version-one database fences for feedback task confirmation.

Triggers cover ORM and bulk writers in the same transaction on both databases.
Keep these definitions stable: Alembic revision 177 imports this version.
"""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text

TASK_FIELDS = (
    "status",
    "attempt_number",
    "workflow_state",
    "workflow_id",
    "agent_id",
    "agent_profile_id",
    "project_id",
    "description",
    "expected_output",
)


def install_feedback_safety(connection: Connection) -> None:
    """Install portable safety generation and exact-attempt settlement fences."""
    if "feedback_state_generation" not in {
        col["name"] for col in inspect(connection).get_columns("tasks")
    }:
        connection.execute(
            text(
                "ALTER TABLE tasks ADD COLUMN feedback_state_generation INTEGER NOT NULL DEFAULT 0"
            )
        )
    settlement = (
        "CASE WHEN NEW.attempt_number <> OLD.attempt_number THEN 'cancelled' "
        "WHEN NEW.status = 'done' THEN 'completed' ELSE NEW.status END"
    )
    settle_receipts = (
        f"UPDATE task_revision_receipts SET state = {settlement} "
        "WHERE task_id = NEW.task_id AND attempt_number = OLD.attempt_number "
        "AND state NOT IN ('completed','done','failed','cancelled'); "
        f"UPDATE feedback_work_links SET state = {settlement} "
        "WHERE kind = 'child_task' AND reference_id = NEW.task_id "
        "AND attempt_number = OLD.attempt_number "
        "AND COALESCE(state, '') NOT IN ('completed','done','failed','cancelled'); "
    )
    if connection.dialect.name == "sqlite":
        condition = " OR ".join(f"OLD.{name} IS NOT NEW.{name}" for name in TASK_FIELDS)
        connection.execute(text("DROP TRIGGER IF EXISTS feedback_task_settlement"))
        connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS feedback_task_safety AFTER UPDATE ON tasks "
                f"WHEN {condition} BEGIN UPDATE tasks SET feedback_state_generation = "
                "feedback_state_generation + 1 WHERE task_id = NEW.task_id; END"
            )
        )
        for table in ("step_runs", "notifications"):
            for action, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                connection.execute(
                    text(
                        f"CREATE TRIGGER IF NOT EXISTS feedback_{table}_{action.lower()} "
                        f"AFTER {action} ON {table} BEGIN UPDATE tasks "
                        "SET feedback_state_generation = feedback_state_generation + 1 "
                        f"WHERE task_id = {row}.task_id; END"
                    )
                )
        connection.execute(
            text(
                "CREATE TRIGGER feedback_task_settlement AFTER UPDATE OF status,attempt_number "
                f"ON tasks BEGIN {settle_receipts} END"
            )
        )
        return
    condition = " OR ".join(
        f"OLD.{name}::text IS DISTINCT FROM NEW.{name}::text" for name in TASK_FIELDS
    )
    connection.execute(
        text(
            "CREATE OR REPLACE FUNCTION feedback_task_safety_v1() RETURNS trigger "
            f"LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN "
            "NEW.feedback_state_generation := OLD.feedback_state_generation + 1; END IF; "
            "RETURN NEW; END $$"
        )
    )
    connection.execute(text("DROP TRIGGER IF EXISTS feedback_task_safety ON tasks"))
    connection.execute(
        text(
            "CREATE TRIGGER feedback_task_safety BEFORE UPDATE ON tasks "
            "FOR EACH ROW EXECUTE FUNCTION feedback_task_safety_v1()"
        )
    )
    connection.execute(
        text(
            "CREATE OR REPLACE FUNCTION feedback_related_safety_v1() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF TG_OP = 'DELETE' THEN "
            "UPDATE tasks SET feedback_state_generation = feedback_state_generation + 1 "
            "WHERE task_id = OLD.task_id; RETURN OLD; ELSE "
            "UPDATE tasks SET feedback_state_generation = feedback_state_generation + 1 "
            "WHERE task_id = NEW.task_id; RETURN NEW; END IF; END $$"
        )
    )
    for table in ("step_runs", "notifications"):
        connection.execute(text(f"DROP TRIGGER IF EXISTS feedback_related_safety ON {table}"))
        connection.execute(
            text(
                f"CREATE TRIGGER feedback_related_safety AFTER INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION feedback_related_safety_v1()"
            )
        )
    connection.execute(
        text(
            "CREATE OR REPLACE FUNCTION feedback_task_settlement_v1() RETURNS trigger "
            f"LANGUAGE plpgsql AS $$ BEGIN {settle_receipts} RETURN NEW; END $$"
        )
    )
    connection.execute(text("DROP TRIGGER IF EXISTS feedback_task_settlement ON tasks"))
    connection.execute(
        text(
            "CREATE TRIGGER feedback_task_settlement AFTER UPDATE OF status,attempt_number ON tasks "
            "FOR EACH ROW EXECUTE FUNCTION feedback_task_settlement_v1()"
        )
    )


def uninstall_feedback_safety(connection: Connection) -> None:
    """Remove version-one triggers before downgrading receipt tables."""
    if connection.dialect.name == "sqlite":
        for name in ("feedback_task_safety", "feedback_task_settlement"):
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        for table in ("step_runs", "notifications"):
            for action in ("insert", "update", "delete"):
                connection.execute(text(f"DROP TRIGGER IF EXISTS feedback_{table}_{action}"))
        return
    for name, table in (
        ("feedback_task_safety", "tasks"),
        ("feedback_task_settlement", "tasks"),
        ("feedback_related_safety", "step_runs"),
        ("feedback_related_safety", "notifications"),
    ):
        connection.execute(text(f"DROP TRIGGER IF EXISTS {name} ON {table}"))
    for name in (
        "feedback_task_safety_v1",
        "feedback_related_safety_v1",
        "feedback_task_settlement_v1",
    ):
        connection.execute(text(f"DROP FUNCTION IF EXISTS {name}()"))
