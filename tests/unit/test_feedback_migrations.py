from __future__ import annotations

import importlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from cognis.bootstrap import run_schema_bootstrap
from cognis.store import feedback_models as rows
from cognis.store.models import Base, DocumentUserGrant

MIGRATIONS = (
    "176_work_feedback",
    "177_task_revision_receipts",
    "178_feedback_capabilities",
    "180_document_sharing",
)
TABLES = (DocumentUserGrant.metadata.tables["document_user_grants"],) + tuple(
    row.__table__ for row in rows.FOUNDATION_TABLES + rows.RECEIPT_TABLES + rows.CAPABILITY_TABLES
)


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def engine(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    if request.param == "sqlite":
        database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
        try:
            yield database
        finally:
            await database.dispose()
        return
    url = os.getenv("COGNIS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
    schema = f"feedback_{uuid4().hex}"
    control = create_async_engine(url, poolclass=NullPool)
    async with control.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    database = create_async_engine(
        url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        yield database
    finally:
        await database.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await control.dispose()


def previous_schema(connection: Connection) -> None:
    excluded = {table.name for table in TABLES}
    Base.metadata.create_all(
        connection,
        tables=[table for table in Base.metadata.sorted_tables if table.name not in excluded],
    )
    connection.execute(text("ALTER TABLE tasks DROP COLUMN feedback_state_generation"))


def migrate(connection: Connection, *, downgrade: bool = False) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        for name in reversed(MIGRATIONS) if downgrade else MIGRATIONS:
            module = importlib.import_module(f"cognis.store.migrations.versions.{name}")
            module.downgrade() if downgrade else module.upgrade()


def assert_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    errors = []
    for table in TABLES:
        if table.name not in inspector.get_table_names():
            errors.append(f"{table.name}: missing table")
            continue
        actual = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(actual) != set(table.columns.keys()):
            errors.append(
                f"{table.name} missing={set(table.columns.keys()) - set(actual)} "
                f"extra={set(actual) - set(table.columns.keys())}"
            )
        for column in table.columns:
            if column.name not in actual:
                continue
            if actual[column.name]["nullable"] != column.nullable:
                errors.append(f"{table.name}.{column.name}: nullability differs")
            if actual[column.name]["type"].compile(dialect=connection.dialect).upper() != (
                column.type.compile(dialect=connection.dialect).upper()
            ):
                errors.append(f"{table.name}.{column.name}: type differs")
        actual_indexes = {
            (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(table.name)
            if not index.get("duplicates_constraint")
        }
        expected_indexes = {
            (tuple(column.name for column in index.columns), index.unique)
            for index in table.indexes
        }
        if expected_indexes != actual_indexes:
            errors.append(f"{table.name}: indexes {actual_indexes} != {expected_indexes}")
        if inspector.get_pk_constraint(table.name)["constrained_columns"] != [
            column.name for column in table.primary_key.columns
        ]:
            errors.append(f"{table.name}: primary key differs")
    generation = next(
        column
        for column in inspector.get_columns("tasks")
        if column["name"] == "feedback_state_generation"
    )
    assert generation["nullable"] is False
    assert not errors, "\n".join(errors)


@pytest.mark.asyncio
async def test_migrations_match_metadata_and_bootstrap_twice(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(previous_schema)
        await connection.run_sync(migrate)
        await connection.run_sync(assert_schema)
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    async with engine.begin() as connection:
        await connection.run_sync(assert_schema)
        await connection.run_sync(lambda sync: migrate(sync, downgrade=True))
        names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert not names.intersection(table.name for table in TABLES)
        await connection.run_sync(migrate)
        await connection.run_sync(assert_schema)


@pytest.mark.asyncio
async def test_bootstrap_upgrades_previous_schema_idempotently(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(previous_schema)
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    async with engine.begin() as connection:
        await connection.run_sync(assert_schema)
