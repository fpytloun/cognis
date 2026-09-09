from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_channel_delivery_route_release_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "channel-delivery-route-release.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "147_session_runtime_overrides")

    command.upgrade(config, "148_channel_delivery_route_release")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("channel_delivery_outbox")
        }
        assert {"route_released_at", "route_release_audit"}.issubset(columns)
    finally:
        engine.dispose()

    command.downgrade(config, "147_session_runtime_overrides")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("channel_delivery_outbox")
        }
        assert "route_released_at" not in columns
        assert "route_release_audit" not in columns
    finally:
        engine.dispose()
