"""Upgrade preceding 181 without changing cumulative Cognis session totals."""

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection

from cognis.store.usage.models import UsageSessionTotalRow
from cognis.store.usage.schema import (
    ensure_usage_schema,
    rollback_usage_maintenance_lifecycle,
    rollback_usage_proxy_sessions,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_previous_181_proxy_upgrade_and_bootstrap_are_idempotent(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    data = h.event().model_dump()
    data["context"]["attribution"]["resources"] = {
        "session_id": "verified_legacy_session",
        "provenance": "controller",
    }
    assert h.writer.try_emit(type(h.event()).model_validate(data))
    await h.pump()
    async with h.engine.begin() as connection:
        await connection.run_sync(rollback_usage_maintenance_lifecycle)
        await connection.run_sync(rollback_usage_proxy_sessions)

        def upgrade(sync: Connection) -> None:
            inspector = inspect(sync)
            assert "session_kind" not in {
                item["name"] for item in inspector.get_columns("usage_session_totals")
            }
            assert "client_session_id" not in {
                item["name"] for item in inspector.get_columns("usage_attempts")
            }
            migration = import_module("cognis.store.migrations.versions.182_usage_proxy_sessions")
            assert migration.down_revision == "181_usage_query_contract"
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
            ensure_usage_schema(sync)
            ensure_usage_schema(sync)
            inspector = inspect(sync)
            for table, index in (
                ("usage_attempts", "ix_usage_client_session_time"),
                ("usage_session_totals", "ix_usage_proxy_session"),
            ):
                assert index in {item["name"] for item in inspector.get_indexes(table)}
            assert "ck_usage_session_correlation" in {
                item["name"] for item in inspector.get_check_constraints("usage_session_totals")
            }
            for table in ("usage_hourly_totals", "usage_daily_totals", "usage_archived_totals"):
                columns = {item["name"] for item in inspector.get_columns(table)}
                assert not columns.intersection({"client_id", "client_session_id", "session_kind"})

        await connection.run_sync(upgrade)
    async with h.sessions() as session:
        row = (await session.scalars(select(UsageSessionTotalRow))).one()
        assert row.session_kind == "cognis"
        assert row.session_id == "verified_legacy_session"
        assert row.client_id is None and row.client_session_id is None
        assert row.finalized_count == 1
