from __future__ import annotations

import json

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_revision_110_backfills_only_canonical_top_level_lineage(tmp_path) -> None:
    database = tmp_path / "lineage.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    config.config_file_name = None
    command.upgrade(config, "109_task_control_conversation")
    engine = create_engine(f"sqlite:///{database}")
    base = {
        "user_email": "u@example.com",
        "agent_id": "agent",
        "title_source": "unset",
        "context_type": "web",
        "status": "active",
        "active_executor_generation": 0,
    }
    payloads = {
        "fork": {
            "forked_from": "conversation",
            "forked_from_conversation_id": "source",
            "forked_from_session_id": "session",
        },
        "task": {
            "forked_from": "task",
            "task_id": "task-1",
            "source_step_run_id": "step-1",
            "source_session_id": "session",
        },
        "step": {
            "forked_from": "task_step",
            "task_id": "task-1",
            "step_run_id": "step-1",
            "source_session_id": "session",
        },
        "forged-task": {
            "forked_from": "task",
            "task_id": "task-1",
            "source_step_run_id": "step-1",
            "source_session_id": "session",
        },
        "malicious": {
            "nested": {
                "forked_from": "conversation",
                "forked_from_conversation_id": "secret",
            }
        },
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO conversations (
                    conversation_id, user_email, agent_id, title_source,
                    context_type, status, active_executor_generation,
                    created_at, updated_at
                ) VALUES (
                    'source', 'u@example.com', 'agent', 'unset',
                    'web', 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        for identifier, context_data in payloads.items():
            connection.execute(
                text(
                    """
                    INSERT INTO conversations (
                        conversation_id, user_email, agent_id, title_source,
                        context_type, context_data, status,
                        active_executor_generation, created_at, updated_at
                    ) VALUES (
                        :conversation_id, :user_email, :agent_id, :title_source,
                        :context_type, :context_data, :status,
                        :active_executor_generation, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                ),
                {**base, "conversation_id": identifier, "context_data": json.dumps(context_data)},
            )
        connection.execute(
            text(
                """
                INSERT INTO sessions (
                    session_id, conversation_id, user_email, agent_id, status,
                    delegation_metadata, started_at, updated_at
                ) VALUES
                    ('session', 'source', 'u@example.com', 'agent', 'completed',
                     '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('fork-root', 'fork', 'u@example.com', 'agent', 'active',
                     '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('task-root', 'task', 'u@example.com', 'agent', 'active',
                     '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('step-root', 'step', 'u@example.com', 'agent', 'active',
                     '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('forged-root', 'forged-task', 'u@example.com', 'agent', 'active',
                     '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text("UPDATE sessions SET previous_session_id='session' WHERE session_id='fork-root'")
        )
        connection.execute(
            text(
                "UPDATE sessions SET previous_session_id='session' "
                "WHERE session_id IN ('task-root', 'step-root')"
            )
        )
        connection.execute(
            text(
                """
                UPDATE conversations
                SET active_session_id = CASE conversation_id
                    WHEN 'fork' THEN 'fork-root'
                    WHEN 'task' THEN 'task-root'
                    WHEN 'step' THEN 'step-root'
                    WHEN 'forged-task' THEN 'forged-root'
                END
                WHERE conversation_id IN ('fork', 'task', 'step', 'forged-task')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    task_id, title, status, priority, created_by, agent_id,
                    source_type, delivery_mode, queue_name,
                    active_executor_generation, created_at, updated_at
                ) VALUES (
                    'task-1', 'Task', 'running', 0, 'u@example.com', 'agent',
                    'api', 'same_conversation', 'default', 0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO step_runs (
                    step_run_id, task_id, step_name, step_type, status,
                    attempt, attempt_number, agent_id, session_id, updated_at
                ) VALUES (
                    'step-1', 'task-1', 'implement', 'run', 'completed',
                    1, 1, 'agent', 'session', CURRENT_TIMESTAMP
                )
                """
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        rows = {
            row["conversation_id"]: row
            for row in connection.execute(
                text(
                    """
                    SELECT conversation_id, lineage_kind,
                           fork_source_conversation_id, fork_source_session_id,
                           lineage_task_id, lineage_step_run_id
                    FROM conversations
                    """
                )
            ).mappings()
        }
        assert rows["fork"]["fork_source_conversation_id"] == "source"
        assert rows["fork"]["fork_source_session_id"] == "session"
        assert rows["task"]["lineage_task_id"] == "task-1"
        assert rows["task"]["lineage_step_run_id"] == "step-1"
        assert rows["step"]["lineage_task_id"] == "task-1"
        assert rows["step"]["lineage_step_run_id"] == "step-1"
        assert rows["forged-task"]["lineage_task_id"] is None
        assert rows["malicious"]["lineage_kind"] is None
        conversation_indexes = {
            item["name"] for item in inspect(connection).get_indexes("conversations")
        }
        task_indexes = {item["name"] for item in inspect(connection).get_indexes("tasks")}
        assert "ix_conversations_owner_fork_conversation" in conversation_indexes
        assert "ix_tasks_owner_source_ref" in task_indexes
