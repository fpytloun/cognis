from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_channel_recipient_intents_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "channel-recipient-intents.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "120_schedule_fire_kinds")

    command.upgrade(config, "121_channel_recipient_intents")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        assert "channel_recipient_intents" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("channel_recipient_intents")}
        assert {
            "normalized_address",
            "authorized_artifacts_json",
            "payload_json",
            "resolution_state",
            "side_effect_certainty",
        }.issubset(columns)
        assert {
            "ix_channel_recipient_intents_route",
            "ix_channel_recipient_intents_state",
        }.issubset({index["name"] for index in inspector.get_indexes("channel_recipient_intents")})
    finally:
        engine.dispose()

    command.downgrade(config, "120_schedule_fire_kinds")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        assert "channel_recipient_intents" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
