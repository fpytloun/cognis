"""Previous-schema coverage for both runtime-override upgrade paths."""

from __future__ import annotations

import importlib
import os
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from cognis.bootstrap import _ensure_session_runtime_override_columns


@pytest.mark.parametrize("upgrade_path", ["bootstrap", "alembic"])
async def test_runtime_override_schema_upgrade(tmp_path, upgrade_path):
    url = os.environ.get("COGNIS_TEST_POSTGRES_URL")
    schema = f"runtime_schema_{uuid.uuid4().hex}"
    admin = None
    if url:
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/schema.db")

    def exercise(conn):
        conn.execute(text("CREATE TABLE sessions (session_id VARCHAR PRIMARY KEY)"))
        conn.execute(text("INSERT INTO sessions (session_id) VALUES ('existing-session')"))
        migration = importlib.import_module(
            "cognis.store.migrations.versions.147_session_runtime_overrides"
        )
        if upgrade_path == "bootstrap":
            _ensure_session_runtime_override_columns(conn)
            _ensure_session_runtime_override_columns(conn)
        else:
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
        columns = {column["name"] for column in inspect(conn).get_columns("sessions")}
        assert columns == {
            "session_id",
            "model_override",
            "model_override_provider_id",
            "reasoning_effort_override",
            "fast_mode_override",
            "runtime_override_revision",
        }
        row = conn.execute(
            text(
                "SELECT model_override, fast_mode_override, runtime_override_revision FROM sessions"
            )
        ).one()
        assert tuple(row) == (None, None, 0)
        if upgrade_path == "alembic":
            with Operations.context(MigrationContext.configure(conn)):
                migration.downgrade()
            assert {column["name"] for column in inspect(conn).get_columns("sessions")} == {
                "session_id"
            }

    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
