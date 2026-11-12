"""Previous-182 bootstrap/formal migration and rollback preserve canonical facts."""

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from cognis.store.usage.models import UsageAttemptRow
from cognis.store.usage.schema import (
    MAINTENANCE_INDEXES,
    ensure_usage_schema,
    rollback_usage_maintenance_lifecycle,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_previous_182_lifecycle_upgrade_and_bootstrap_are_idempotent(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    event = h.event(proxy="schema_retained")
    assert h.writer.try_emit(event)
    await h.pump()
    async with h.engine.begin() as connection:
        await connection.run_sync(rollback_usage_maintenance_lifecycle)

        def upgrade(sync):
            assert "oldest_detail_at" not in {
                item["name"] for item in inspect(sync).get_columns("usage_projection_state")
            }
            migration = import_module(
                "cognis.store.migrations.versions.183_usage_maintenance_lifecycle"
            )
            assert migration.down_revision == "182_usage_proxy_sessions"
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
            ensure_usage_schema(sync)
            ensure_usage_schema(sync)
            for table, names in MAINTENANCE_INDEXES.items():
                assert set(names) <= {item["name"] for item in inspect(sync).get_indexes(table)}

        await connection.run_sync(upgrade)
    async with h.sessions() as session:
        row = (await session.scalars(select(UsageAttemptRow))).one()
        assert row.attempt_id == event.context.trace.attempt_id
        assert row.client_session_id == "schema_retained"
        assert row.fact_version == row.projected_version == 1
