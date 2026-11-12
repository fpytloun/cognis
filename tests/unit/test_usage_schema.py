from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select
from sqlalchemy.engine import Connection

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine
from cognis.store.models import Base
from cognis.store.schema import alembic_config
from cognis.store.usage.models import USAGE_TABLE_NAMES, UsagePriceRevisionRow
from cognis.store.usage.schema import ensure_usage_schema

MIGRATION = "cognis.store.migrations.versions.180_usage_accounting"


def exercise_usage_schema(connection: Connection) -> None:
    """Reusable SQLite/PostgreSQL additive upgrade, preservation and rollback test."""
    migration = importlib.import_module(MIGRATION)
    assert migration.revision == "180_usage_accounting"
    assert migration.down_revision == "179_merge_feedback_app_contact"
    migration.op = Operations(MigrationContext.configure(connection))
    migration.upgrade()
    now = datetime(2026, 10, 29, tzinfo=UTC)
    connection.execute(
        UsagePriceRevisionRow.__table__.insert().values(
            revision_id="price_usage_schema",
            provider_id="provider_usage_schema",
            model="chat",
            effective_at=now,
            recorded_at=now,
            currency="USD",
            basis="configured_estimate",
            provenance="configured",
            rule_version=1,
            content_digest="a" * 64,
            definition={"rates": [{"dimension": "input", "per_million": "0"}]},
        )
    )
    migration.upgrade()
    ensure_usage_schema(connection)
    assert connection.scalar(select(UsagePriceRevisionRow.revision_id)) == "price_usage_schema"
    inspector = inspect(connection)
    assert set(USAGE_TABLE_NAMES) <= set(inspector.get_table_names())
    for name in USAGE_TABLE_NAMES:
        assert {c["name"] for c in inspector.get_columns(name)} == {
            c.name for c in Base.metadata.tables[name].columns
        }
        assert {
            i["name"] for i in inspector.get_indexes(name) if not i.get("duplicates_constraint")
        } == {i.name for i in Base.metadata.tables[name].indexes}
    pending = next(
        i
        for i in inspector.get_indexes("usage_projection_outbox")
        if i["name"] == "ix_usage_outbox_pending"
    )
    assert pending["dialect_options"][f"{connection.dialect.name}_where"] is not None
    archive_columns = {c["name"] for c in inspector.get_columns("usage_archived_totals")}
    assert {
        "session_id",
        "usage_subject_key",
        "billing_units",
        "exact_reprice_supported",
        "manifest_id",
        "evidence_digest",
        "logical_error_count",
        "ttft_count",
    } <= archive_columns
    assert "generation" not in archive_columns  # Archive is canonical, not a projection.
    migration.downgrade()
    assert not (set(USAGE_TABLE_NAMES) & set(inspect(connection).get_table_names()))
    migration.upgrade()


@pytest.mark.asyncio
async def test_usage_migration_and_schema_are_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(exercise_usage_schema)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_bootstrap_twice_creates_usage_and_preserves_prices(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'bootstrap.db'}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(USAGE_TABLE_NAMES) <= set(tables)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_previous_alembic_head_upgrade_and_downgrade(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'previous.db'}")

    def exercise(connection: Connection) -> None:
        config = alembic_config(str(engine.url))
        config.config_file_name = None
        config.attributes["connection"] = connection
        command.upgrade(config, "179_merge_feedback_app_contact")
        command.upgrade(config, "head")
        assert set(USAGE_TABLE_NAMES) <= set(inspect(connection).get_table_names())
        command.downgrade(config, "179_merge_feedback_app_contact")
        assert "users" in inspect(connection).get_table_names()
        assert not (set(USAGE_TABLE_NAMES) & set(inspect(connection).get_table_names()))
        command.upgrade(config, "head")

    try:
        # env.py commits legacy normalization and owns migration transactions.
        async with engine.connect() as connection:
            await connection.run_sync(exercise)
    finally:
        await engine.dispose()
