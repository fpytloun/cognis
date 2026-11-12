"""Nullable plan metadata upgrades existing jobs on SQLite and PostgreSQL."""

from __future__ import annotations

import importlib
import os
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import run_schema_bootstrap


def snapshot(connection):
    columns = {column["name"]: column for column in inspect(connection).get_columns("audio_jobs")}
    row = connection.execute(
        text(
            "SELECT job_id, target_minutes, status, length_preference, target_words FROM audio_jobs"
        )
    ).one()
    return columns, row


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("method", ["bootstrap", "migration"])
async def test_plan_upgrade_preserves_legacy_recordings(tmp_path, backend, method):
    admin = None
    schema = f"audio_plans_{uuid.uuid4().hex}"
    if backend == "postgresql":
        url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("needs isolated COGNIS_TEST_POSTGRES_URL")
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/plans.db")
    module = importlib.import_module("cognis.store.migrations.versions.175_audio_length_plans")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE audio_jobs (job_id VARCHAR(64) PRIMARY KEY, "
                    "target_minutes INTEGER NOT NULL, status VARCHAR(32) NOT NULL)"
                )
            )
            await connection.execute(
                text("INSERT INTO audio_jobs VALUES ('legacy-recording', 6, 'completed')")
            )
        for _ in range(2):
            if method == "bootstrap":
                await run_schema_bootstrap(engine)
            else:
                async with engine.begin() as connection:

                    def upgrade(sync):
                        with Operations.context(MigrationContext.configure(sync)):
                            module.upgrade()

                    await connection.run_sync(upgrade)
        async with engine.connect() as connection:
            columns, row = await connection.run_sync(snapshot)
        assert columns["target_words"]["nullable"]
        assert columns["length_preference"]["nullable"]
        assert tuple(row) == ("legacy-recording", 6, "completed", None, None)
        async with engine.begin() as connection:

            def downgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    module.downgrade()
                return {column["name"] for column in inspect(sync).get_columns("audio_jobs")}

            columns_after = await connection.run_sync(downgrade)
            assert "target_words" not in columns_after
            assert "length_preference" not in columns_after
            assert await connection.scalar(text("SELECT target_minutes FROM audio_jobs")) == 6
    finally:
        await engine.dispose()
        if admin:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
