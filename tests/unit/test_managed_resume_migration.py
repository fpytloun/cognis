from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_managed_resume_prepared_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "managed-resume-migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")
    command.upgrade(config, "115_managed_channel_resume")

    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("managed_conversation_signals")
        }
        assert "resume_prepared_at" not in columns
    finally:
        engine.dispose()

    command.upgrade(config, "116_managed_resume_prepared")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("managed_conversation_signals")
        }
        assert "resume_prepared_at" in columns
    finally:
        engine.dispose()

    command.downgrade(config, "115_managed_channel_resume")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("managed_conversation_signals")
        }
        assert "resume_prepared_at" not in columns
    finally:
        engine.dispose()
