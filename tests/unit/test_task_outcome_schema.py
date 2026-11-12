from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, delete, insert, inspect, select, update

from cognis.store.app_channels import ensure_app_channels
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, ChannelAccountRow, Task
from cognis.store.queries import (
    create_agent,
    create_channel_account,
    create_user,
    delete_channel_account,
)
from cognis.store.task_outcome_schema import ensure_task_outcome_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_task_outcome_bootstrap_and_migration_preserve_data(tmp_path: Path, backend: str):
    url = f"sqlite+aiosqlite:///{tmp_path / 'outcome-upgrade.db'}"
    if backend == "postgresql":
        url = os.environ.get("COGNIS_TASK_OUTCOME_TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("COGNIS_TASK_OUTCOME_TEST_POSTGRES_URL is not configured")
    engine = create_engine(url)
    factory = create_session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            await create_user(
                db, email="schema@example.com", name="Schema", password_hash="hash", role="user"
            )
            await create_agent(
                db, agent_id="schema-agent", owner_email="schema@example.com", name="Schema agent"
            )
            await db.execute(
                delete(ChannelAccountRow).where(ChannelAccountRow.channel_type == "web")
            )
            await create_channel_account(
                db,
                account_id="schema-signal",
                channel_type="signal",
                display_name="Signal",
                user_email="schema@example.com",
                agent_id="schema-agent",
                preferred_for_task_delivery=True,
            )
            await db.commit()
        async with engine.begin() as connection:
            for table in ("tasks", "schedules"):
                await connection.exec_driver_sql(
                    f"ALTER TABLE {table} DROP COLUMN on_finish_instructions"
                )
            await connection.run_sync(ensure_task_outcome_schema)
            await connection.run_sync(ensure_task_outcome_schema)
            await connection.run_sync(ensure_app_channels)
            await connection.run_sync(ensure_app_channels)

            migration = importlib.import_module(
                "cognis.store.migrations.versions.176_task_outcomes"
            )

            def migrate(sync: Connection) -> None:
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
                    migration.upgrade()
                    for table in ("tasks", "schedules"):
                        assert "on_finish_instructions" in {
                            column["name"] for column in inspect(sync).get_columns(table)
                        }
                    sync.execute(
                        insert(Task).values(
                            task_id="schema-task",
                            title="Check",
                            description="Verify",
                            created_by="schema@example.com",
                            agent_id="schema-agent",
                            status="draft",
                            source_type="api",
                            delivery_mode="preferred_channel",
                            on_finish_instructions="Inspect failures",
                        )
                    )
                    migration.downgrade()
                    for table in ("tasks", "schedules"):
                        assert "on_finish_instructions" not in {
                            column["name"] for column in inspect(sync).get_columns(table)
                        }
                    assert (
                        sync.exec_driver_sql(
                            "SELECT title FROM tasks WHERE task_id = 'schema-task'"
                        ).scalar_one()
                        == "Check"
                    )
                    migration.upgrade()

            await connection.run_sync(migrate)
        async with factory() as db:
            rows = list((await db.scalars(select(ChannelAccountRow))).all())
            assert len([row for row in rows if row.channel_type == "web" and row.enabled]) == 1
            assert [row.account_id for row in rows if row.preferred_for_task_delivery] == [
                "schema-signal"
            ]
            await delete_channel_account(db, "schema-signal")
            await db.execute(
                update(ChannelAccountRow)
                .where(ChannelAccountRow.channel_type == "web")
                .values(preferred_for_task_delivery=False)
            )
            await db.commit()
        async with engine.begin() as connection:
            await connection.run_sync(ensure_app_channels)
            await connection.run_sync(ensure_app_channels)
        async with factory() as db:
            rows = list((await db.scalars(select(ChannelAccountRow))).all())
            assert len(rows) == 1
            assert rows[0].channel_type == "web" and rows[0].preferred_for_task_delivery
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
