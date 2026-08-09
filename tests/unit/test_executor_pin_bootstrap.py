from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Agent, Base, Conversation, Task, User


@pytest.mark.asyncio
async def test_stage3_bootstrap_upgrades_previous_shape_idempotently(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/stage3-bootstrap.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        session.add(User(email="u@example.com", name="U", role="user"))
        await session.flush()
        session.add(Agent(agent_id="a", owner_email="u@example.com", name="A"))
        await session.flush()
        session.add(
            Conversation(
                conversation_id="conv",
                user_email="u@example.com",
                agent_id="a",
                context_type="chat",
            )
        )
        session.add(
            Task(
                task_id="task",
                title="T",
                created_by="u@example.com",
                agent_id="a",
            )
        )
        await session.commit()

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE executor_pin_notice_outbox"))
        await conn.execute(text("DROP TABLE executor_pin_transitions"))
        for table_name in ("conversations", "tasks"):
            await conn.execute(
                text(f"ALTER TABLE {table_name} DROP COLUMN active_executor_unavailable_since")
            )
            await conn.execute(
                text(f"ALTER TABLE {table_name} DROP COLUMN active_executor_generation")
            )

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.connect() as conn:
        schema = await conn.run_sync(
            lambda sync_conn: {
                "tables": set(inspect(sync_conn).get_table_names()),
                "conversation_columns": {
                    column["name"] for column in inspect(sync_conn).get_columns("conversations")
                },
                "task_columns": {
                    column["name"] for column in inspect(sync_conn).get_columns("tasks")
                },
                "transition_indexes": {
                    index["name"]
                    for index in inspect(sync_conn).get_indexes("executor_pin_transitions")
                },
                "outbox_indexes": {
                    index["name"]
                    for index in inspect(sync_conn).get_indexes("executor_pin_notice_outbox")
                },
            }
        )
        conversation_generation = await conn.scalar(
            text(
                "SELECT active_executor_generation FROM conversations "
                "WHERE conversation_id = 'conv'"
            )
        )
        task_generation = await conn.scalar(
            text("SELECT active_executor_generation FROM tasks WHERE task_id = 'task'")
        )

    assert {"executor_pin_transitions", "executor_pin_notice_outbox"} <= schema["tables"]
    assert {
        "active_executor_generation",
        "active_executor_unavailable_since",
    } <= schema["conversation_columns"]
    assert {
        "active_executor_generation",
        "active_executor_unavailable_since",
    } <= schema["task_columns"]
    assert "ix_executor_pin_transitions_notice_pending" in schema["transition_indexes"]
    assert "ix_executor_pin_notice_outbox_pending" in schema["outbox_indexes"]
    assert conversation_generation == task_generation == 0
    await engine.dispose()
