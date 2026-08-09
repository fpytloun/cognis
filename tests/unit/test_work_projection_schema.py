from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine


def test_work_category_migration_and_bootstrap_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "work-category-migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "126_task_delivery_preferred_channel_default")

    async_engine = create_engine(f"sqlite+aiosqlite:///{database_path}")
    asyncio.run(run_schema_bootstrap(async_engine))
    asyncio.run(run_schema_bootstrap(async_engine))
    asyncio.run(async_engine.dispose())
    command.upgrade(config, "127_work_record_categories")

    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        tables = set(inspect(engine).get_table_names())
        columns = {column["name"] for column in inspect(engine).get_columns("work_records")}
        indexes = {index["name"] for index in inspect(engine).get_indexes("work_records")}
    finally:
        engine.dispose()
    assert {"category", "entity_id", "file_path_ids", "additions", "deletions"} <= columns
    assert "work_record_files" in tables
    assert "ix_work_records_owner_category_order" in indexes
    assert "ix_work_records_owner_category_entity" in indexes


@pytest.mark.asyncio
async def test_work_projection_bootstrap_is_idempotent_and_creates_indexes(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'work.db'}")

    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)

    async with engine.connect() as connection:
        tables, indexes, columns = await connection.run_sync(
            lambda sync_connection: (
                set(inspect(sync_connection).get_table_names()),
                {
                    table: {
                        index["name"]: index["column_names"]
                        for index in inspect(sync_connection).get_indexes(table)
                    }
                    for table in (
                        "work_records",
                        "work_record_files",
                        "work_session_projections",
                    )
                },
                {
                    table: {
                        column["name"] for column in inspect(sync_connection).get_columns(table)
                    }
                    for table in (
                        "work_records",
                        "work_record_files",
                        "work_session_projections",
                    )
                },
            )
        )
    assert {"work_records", "work_record_files", "work_session_projections"} <= tables
    assert {
        "ix_work_record_files_record_order",
        "ix_work_record_files_path",
    } <= set(indexes["work_record_files"])
    assert {
        "ix_work_records_owner_session_version_order",
        "ix_work_records_owner_version_newest",
        "ix_work_records_pairing",
        "ix_work_records_owner_category_order",
        "ix_work_records_owner_category_entity",
    } <= set(indexes["work_records"])
    assert {
        "ix_work_session_projections_queue",
        "ix_work_session_projections_owner_state",
        "ix_work_session_projections_lease",
    } <= set(indexes["work_session_projections"])
    assert "is_evidence" in columns["work_records"]
    assert {"category", "entity_id", "file_path_ids", "additions", "deletions"} <= columns[
        "work_records"
    ]
    assert "head_checked_at" in columns["work_session_projections"]
    assert "is_evidence" in indexes["work_records"]["ix_work_records_owner_version_newest"]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users "
                "(email, name, password_hash, role, created_at, updated_at) "
                "VALUES ('schema@example.com', 'Schema', 'x', 'user', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO agents "
                "(agent_id, owner_email, name, description, sync_metadata, agent_type, "
                "is_system, hidden, status, created_at, updated_at) "
                "VALUES ('schema-agent', 'schema@example.com', 'Schema', '', '{}', "
                "'primary', 0, 0, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO conversations "
                "(conversation_id, user_email, agent_id, title_source, context_type, status, "
                "active_executor_generation, created_at, updated_at) "
                "VALUES ('schema-conversation', 'schema@example.com', 'schema-agent', "
                "'unset', 'web', 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO sessions "
                "(session_id, conversation_id, user_email, agent_id, delegation_metadata, "
                "activity_scope_id, status, started_at, updated_at) "
                "VALUES ('schema-session', 'schema-conversation', 'schema@example.com', "
                "'schema-agent', '{}', 'schema-session', 'active', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO work_session_projections "
                "(projection_id, owner_email, session_id, source_session_id, "
                "materializer_version, created_at, updated_at) "
                "VALUES ('schema-projection', 'schema@example.com', 'schema-session', "
                "'schema-session', 'work-v1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        row = (
            await connection.execute(
                text(
                    "SELECT target_seq, covered_through_seq, state, retry_count, "
                    "lease_fence, priority FROM work_session_projections "
                    "WHERE projection_id = 'schema-projection'"
                )
            )
        ).one()
        assert tuple(row) == (0, 0, "pending", 0, 0, 0)
        await connection.execute(
            text(
                "INSERT INTO work_records "
                "(work_record_id, owner_email, session_id, materializer_version, "
                "source_session_id, source_seq, source_item_id, occurred_at, record_type, "
                "timeline_item) VALUES ('schema-record', 'schema@example.com', "
                "'schema-session', 'work-v1', 'schema-session', 1, 'tool:one', "
                "CURRENT_TIMESTAMP, 'tool_call', '{}')"
            )
        )
        assert (
            await connection.scalar(
                text("SELECT is_evidence FROM work_records WHERE work_record_id = 'schema-record'")
            )
            == 1
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE work_session_projections SET target_seq = -1 "
                    "WHERE projection_id = 'schema-projection'"
                )
            )
    await engine.dispose()
