from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from cognis.bootstrap import _ensure_work_call_state_index, run_schema_bootstrap
from cognis.store.database import create_engine
from cognis.store.models import WorkRecordRow

INDEX_NAME = "ix_work_records_owner_version_call_state"
EXPECTED_COLUMNS = ["owner_email", "materializer_version", "materialized_at"]


def _index(indexes: list[dict[str, object]]) -> dict[str, object]:
    return next(index for index in indexes if index.get("name") == INDEX_NAME)


@pytest.mark.asyncio
async def test_work_call_state_index_bootstrap_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'bootstrap.db'}")
    try:
        await run_schema_bootstrap(engine)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP INDEX {INDEX_NAME}"))
            await connection.execute(
                text(f"CREATE INDEX {INDEX_NAME} ON work_records (owner_email, materialized_at)")
            )
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            indexes = await connection.run_sync(
                lambda sync: inspect(sync).get_indexes("work_records")
            )
    finally:
        await engine.dispose()

    index = _index(indexes)
    assert index["column_names"] == EXPECTED_COLUMNS
    assert index["unique"] == 0
    assert "call_id IS NOT NULL" in str(
        dict(index.get("dialect_options") or {}).get("sqlite_where")
    )


@pytest.mark.asyncio
async def test_migration_139_matches_bootstrap_index(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    migration = importlib.import_module(
        "cognis.store.migrations.versions.139_work_call_state_index"
    )
    try:
        await run_schema_bootstrap(engine)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP INDEX {INDEX_NAME}"))
            await connection.execute(
                text(f"CREATE INDEX {INDEX_NAME} ON work_records (owner_email, materialized_at)")
            )

            def upgrade(sync_connection: object) -> None:
                context = MigrationContext.configure(sync_connection)
                migration.op = Operations(context)
                migration.upgrade()

            await connection.run_sync(upgrade)
            indexes = await connection.run_sync(
                lambda sync: inspect(sync).get_indexes("work_records")
            )
    finally:
        await engine.dispose()

    index = _index(indexes)
    assert index["column_names"] == EXPECTED_COLUMNS
    assert "call_id IS NOT NULL" in str(
        dict(index.get("dialect_options") or {}).get("sqlite_where")
    )
    assert migration.down_revision == "138_schedule_task_failure_options"


def test_work_call_state_index_postgresql_shape() -> None:
    index = next(index for index in WorkRecordRow.__table__.indexes if index.name == INDEX_NAME)
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    assert "ON work_records (owner_email, materializer_version, materialized_at DESC)" in ddl
    assert "INCLUDE (session_id, call_id, is_evidence)" in ddl
    assert "WHERE call_id IS NOT NULL" in ddl


@pytest.mark.asyncio
async def test_postgresql_replaces_wrong_shape_and_accepts_exact_shape() -> None:
    url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
    engine = create_engine(url)
    migration = importlib.import_module(
        "cognis.store.migrations.versions.139_work_call_state_index"
    )

    async def install_wrong_shape() -> None:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
            await connection.execute(
                text(f"CREATE INDEX {INDEX_NAME} ON work_records (owner_email, materialized_at)")
            )

    async def reflected_index() -> dict[str, object]:
        async with engine.connect() as connection:
            indexes = await connection.run_sync(
                lambda sync: inspect(sync).get_indexes("work_records")
            )
        return _index(indexes)

    async def ensure_index() -> None:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.run_sync(_ensure_work_call_state_index)

    try:
        await run_schema_bootstrap(engine)
        await install_wrong_shape()
        await asyncio.gather(
            ensure_index(),
            ensure_index(),
        )
        await ensure_index()
        bootstrap_index = await reflected_index()

        await install_wrong_shape()

        def upgrade(sync_connection: object) -> None:
            context = MigrationContext.configure(sync_connection)
            migration.op = Operations(context)
            with context.begin_transaction():
                migration.upgrade()

        async with engine.connect() as connection:
            await connection.run_sync(upgrade)
        migration_index = await reflected_index()
    finally:
        await engine.dispose()

    for index in (bootstrap_index, migration_index):
        assert index["column_names"] == EXPECTED_COLUMNS
        assert index["column_sorting"] == {"materialized_at": ("desc",)}
        assert index["include_columns"] == ["session_id", "call_id", "is_evidence"]
        assert "call_id IS NOT NULL" in str(
            dict(index.get("dialect_options") or {}).get("postgresql_where")
        )
