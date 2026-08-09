from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine as create_async_engine


def test_group_context_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "group-context-migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "116_managed_resume_prepared")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        ledger_columns = {
            column["name"] for column in inspect(engine).get_columns("channel_inbound_ledger")
        }
        consumption_columns = {
            column["name"] for column in inspect(engine).get_columns("channel_context_consumptions")
        }
        assert "ordering_key" not in ledger_columns
        assert "admitted_turn_id" not in consumption_columns
    finally:
        engine.dispose()

    command.upgrade(config, "117_group_context")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        ledger_columns = {
            column["name"] for column in inspector.get_columns("channel_inbound_ledger")
        }
        consumption_columns = {
            column["name"] for column in inspector.get_columns("channel_context_consumptions")
        }
        ledger_indexes = {
            index["name"] for index in inspector.get_indexes("channel_inbound_ledger")
        }
        consumption_indexes = {
            index["name"] for index in inspector.get_indexes("channel_context_consumptions")
        }
        assert {
            "observed_at",
            "ordering_key",
            "ordering_source",
            "retain_until",
        }.issubset(ledger_columns)
        assert {"usage", "trigger_inbound_id", "admitted_turn_id"}.issubset(consumption_columns)
        assert "ix_channel_inbound_ledger_retention" in ledger_indexes
        assert "ix_channel_context_consumptions_admission" in consumption_indexes
    finally:
        engine.dispose()

    command.downgrade(config, "116_managed_resume_prepared")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        ledger_columns = {
            column["name"] for column in inspector.get_columns("channel_inbound_ledger")
        }
        consumption_columns = {
            column["name"] for column in inspector.get_columns("channel_context_consumptions")
        }
        assert "ordering_key" not in ledger_columns
        assert "admitted_turn_id" not in consumption_columns
    finally:
        engine.dispose()


def test_group_context_migration_accepts_bootstrapped_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "group-context-bootstrap-migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "116_managed_resume_prepared")

    async def _bootstrap() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        try:
            await run_schema_bootstrap(engine)
        finally:
            await engine.dispose()

    asyncio.run(_bootstrap())
    command.upgrade(config, "117_group_context")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        ledger_columns = {
            column["name"] for column in inspector.get_columns("channel_inbound_ledger")
        }
        consumption_columns = {
            column["name"] for column in inspector.get_columns("channel_context_consumptions")
        }
        assert {"observed_at", "ordering_key", "retain_until"}.issubset(ledger_columns)
        assert {"usage", "admitted_turn_id"}.issubset(consumption_columns)
    finally:
        engine.dispose()

    command.downgrade(config, "116_managed_resume_prepared")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        ledger_columns = {
            column["name"] for column in inspector.get_columns("channel_inbound_ledger")
        }
        consumption_columns = {
            column["name"] for column in inspector.get_columns("channel_context_consumptions")
        }
        assert "ordering_key" not in ledger_columns
        assert "admitted_turn_id" not in consumption_columns
    finally:
        engine.dispose()
