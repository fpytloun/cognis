from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_channel_delivery_receipts_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "channel-delivery-receipts.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "117_group_context")

    command.upgrade(config, "118_channel_delivery_receipts")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("channel_delivery_outbox")
        }
        assert {
            "delivery_receipts_json",
            "first_delivered_at",
            "last_delivered_at",
        }.issubset(columns)
        assert "channel_delivery_receipts" in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.downgrade(config, "117_group_context")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("channel_delivery_outbox")
        }
        assert "delivery_receipts_json" not in columns
        assert "first_delivered_at" not in columns
        assert "last_delivered_at" not in columns
        assert "channel_delivery_receipts" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
