from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine as create_async_engine

METADATA_COLUMNS = {
    "status",
    "old_path",
    "old_path_id",
    "binary",
    "generated",
    "truncated",
    "preview_omitted",
}


def _config(database_path: Path) -> Config:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    return config


def _columns(database_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        return {str(column["name"]) for column in inspect(engine).get_columns("work_record_files")}
    finally:
        engine.dispose()


def _indexes(database_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        return {str(index["name"]) for index in inspect(engine).get_indexes("work_record_files")}
    finally:
        engine.dispose()


def test_work_file_metadata_migration_upgrades_and_downgrades_sqlite(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "work-file-metadata.db"
    config = _config(database_path)
    command.upgrade(config, "128_work_load_indexes")
    assert METADATA_COLUMNS.isdisjoint(_columns(database_path))

    command.upgrade(config, "129_work_record_file_metadata")
    assert _columns(database_path) >= METADATA_COLUMNS
    assert "ix_work_record_files_old_path" in _indexes(database_path)

    command.downgrade(config, "128_work_load_indexes")
    assert METADATA_COLUMNS.isdisjoint(_columns(database_path))
    assert "ix_work_record_files_old_path" not in _indexes(database_path)


@pytest.mark.asyncio
async def test_work_file_metadata_bootstrap_upgrades_previous_shape_twice(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bootstrap-work-file-metadata.db"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE work_record_files (
                        work_record_file_id VARCHAR PRIMARY KEY,
                        work_record_id VARCHAR NOT NULL,
                        file_ordinal INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        path_id VARCHAR NOT NULL,
                        additions INTEGER NOT NULL,
                        deletions INTEGER NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO work_record_files (
                        work_record_file_id,
                        work_record_id,
                        file_ordinal,
                        path,
                        path_id,
                        additions,
                        deletions
                    ) VALUES (
                        'file-row',
                        'record-row',
                        0,
                        'src/app.py',
                        'root:src/app.py',
                        3,
                        2
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await run_schema_bootstrap(async_engine)
        await run_schema_bootstrap(async_engine)
        async with async_engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    str(column["name"])
                    for column in inspect(sync_connection).get_columns("work_record_files")
                }
            )
            indexes = await connection.run_sync(
                lambda sync_connection: {
                    str(index["name"])
                    for index in inspect(sync_connection).get_indexes("work_record_files")
                }
            )
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            status,
                            old_path,
                            old_path_id,
                            binary,
                            generated,
                            truncated,
                            preview_omitted
                        FROM work_record_files
                        WHERE work_record_file_id = 'file-row'
                        """
                    )
                )
            ).one()
    finally:
        await async_engine.dispose()

    assert columns >= METADATA_COLUMNS
    assert "ix_work_record_files_old_path" in indexes
    assert tuple(row) == (None, None, None, 0, 0, 0, 0)
