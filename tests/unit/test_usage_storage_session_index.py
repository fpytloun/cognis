"""Previous-183 reversible upgrade and idempotent bootstrap on SQLite/PostgreSQL."""

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from cognis.core.usage.query_metadata import UsageIndexCapabilities
from cognis.store.usage.models import UsageSessionTotalRow
from cognis.store.usage.schema import ensure_usage_schema
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)
INDEX = "ix_usage_session_day"
COLUMNS = ("bucket_day", "owner_key", "generation")


def index_contract(sync):
    installed = {item["name"]: item for item in inspect(sync).get_indexes("usage_session_totals")}
    assert tuple(installed[INDEX]["column_names"]) == COLUMNS
    assert not installed[INDEX].get("unique")
    options = installed[INDEX].get("dialect_options", {})
    assert not options.get("sqlite_where") and not options.get("postgresql_where")
    model = next(item for item in UsageSessionTotalRow.__table__.indexes if item.name == INDEX)
    assert tuple(column.name for column in model.columns) == COLUMNS
    assert model.dialect_options["sqlite"].get("where") is None
    assert model.dialect_options["postgresql"].get("where") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("installation", ["migration", "bootstrap"])
async def test_previous_183_session_index_installation_is_idempotent_and_reversible(
    usage_storage: UsageStorageHarness, installation: str
) -> None:
    h = usage_storage
    assert h.writer.try_emit(h.event(proxy="retained_index_upgrade"))
    await h.pump()
    async with h.sessions() as session:
        before = list((await session.execute(select(UsageSessionTotalRow.__table__))).mappings())
    assert before and before[0]["session_kind"] == "proxy"
    migration = import_module("cognis.store.migrations.versions.184_usage_session_day_index")
    assert migration.down_revision == "183_usage_maintenance_lifecycle"
    async with h.engine.begin() as connection:

        def previous_and_install(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()
                assert INDEX not in {
                    item["name"] for item in inspect(sync).get_indexes("usage_session_totals")
                }
                if installation == "migration":
                    migration.upgrade()
                    migration.upgrade()
            ensure_usage_schema(sync)
            ensure_usage_schema(sync)
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()  # Formal upgrade after bootstrap is also safe.
            index_contract(sync)

        await connection.run_sync(previous_and_install)
    async with h.sessions() as session:
        capabilities = await UsageIndexCapabilities.discover(session)
        assert capabilities.supports("usage_session_totals", ("bucket_day",))
        after = list((await session.execute(select(UsageSessionTotalRow.__table__))).mappings())
    assert after == before
    async with h.engine.begin() as connection:

        def rollback(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()
            assert INDEX not in {
                item["name"] for item in inspect(sync).get_indexes("usage_session_totals")
            }
            # Rollback does not drop the existing per-subject/proxy indexes.
            assert {"ix_usage_session_subject", "ix_usage_proxy_session"} <= {
                item["name"] for item in inspect(sync).get_indexes("usage_session_totals")
            }

        await connection.run_sync(rollback)
    async with h.sessions() as session:
        assert (
            list((await session.execute(select(UsageSessionTotalRow.__table__))).mappings())
            == before
        )
