from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine as create_async_engine


@contextmanager
def _preserve_logging_state() -> Iterator[None]:
    logger_dict = dict(logging.Logger.manager.loggerDict)
    loggers = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    state = {
        logger: (
            logger.disabled,
            logger.level,
            list(logger.handlers),
            logger.propagate,
        )
        for logger in loggers
    }
    try:
        yield
    finally:
        for logger, (disabled, level, handlers, propagate) in state.items():
            logger.disabled = disabled
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
        logging.Logger.manager.loggerDict.clear()
        logging.Logger.manager.loggerDict.update(logger_dict)


def _schema(path: Path) -> dict[str, object]:
    engine = create_sync_engine(f"sqlite:///{path}")
    try:
        inspector = inspect(engine)
        return {
            "columns": {
                column["name"]: (column["nullable"], str(column["type"]))
                for column in inspector.get_columns("schedule_fires")
            },
            "pk": tuple(inspector.get_pk_constraint("schedule_fires")["constrained_columns"]),
            "indexes": {
                index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes("schedule_fires")
            },
            "unique": {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints("schedule_fires")
            },
            "checks": {item["name"] for item in inspector.get_check_constraints("schedule_fires")},
        }
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_schedule_fire_bootstrap_matches_migration_and_is_idempotent(
    tmp_path: Path,
) -> None:
    bootstrap_path = tmp_path / "bootstrap.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{bootstrap_path}")
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    await engine.dispose()

    migration_path = tmp_path / "migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{migration_path}")
    with _preserve_logging_state():
        command.upgrade(config, "120_schedule_fire_kinds")

    assert _schema(bootstrap_path) == _schema(migration_path)
    schema = _schema(bootstrap_path)
    assert schema["pk"] == ("fire_id",)
    assert schema["unique"] == {("schedule_id", "fire_kind", "scheduled_fire_at")}
    assert schema["indexes"] == {
        "ix_schedule_fires_reconcile": ("status", "updated_at"),
        "ix_schedule_fires_task": ("task_id",),
    }
    assert schema["checks"] == {
        "ck_schedule_fires_fire_kind",
        "ck_schedule_fires_status",
    }
    bootstrap_engine = create_sync_engine(f"sqlite:///{bootstrap_path}")
    migration_engine = create_sync_engine(f"sqlite:///{migration_path}")
    try:
        for table_name in ("schedule_catchup_state", "channel_account_operations"):
            assert table_name in inspect(bootstrap_engine).get_table_names()
            bootstrap_columns = {
                column["name"]: (
                    column["nullable"],
                    column["primary_key"],
                    str(column["type"]),
                )
                for column in inspect(bootstrap_engine).get_columns(table_name)
            }
            migration_columns = {
                column["name"]: (
                    column["nullable"],
                    column["primary_key"],
                    str(column["type"]),
                )
                for column in inspect(migration_engine).get_columns(table_name)
            }
            assert bootstrap_columns == migration_columns
    finally:
        bootstrap_engine.dispose()
        migration_engine.dispose()


def test_schedule_fire_kind_upgrade_backfills_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "upgrade.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    with _preserve_logging_state():
        command.upgrade(config, "098_schedule_fires")

    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO schedule_fires (
                    fire_id, schedule_id, scheduled_fire_at, status,
                    attempt_count, created_at, updated_at
                ) VALUES (
                    'fire-1', 'schedule-1', CURRENT_TIMESTAMP, 'claimed',
                    1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
    finally:
        engine.dispose()

    with _preserve_logging_state():
        command.upgrade(config, "120_schedule_fire_kinds")
    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT fire_kind FROM schedule_fires WHERE fire_id = 'fire-1'"
                ).scalar_one()
                == "recurring"
            )
            connection.exec_driver_sql(
                """
                INSERT INTO schedule_fires (
                    fire_id, schedule_id, fire_kind, scheduled_fire_at, status,
                    attempt_count, created_at, updated_at
                )
                SELECT
                    'fire-manual', schedule_id, 'manual', scheduled_fire_at, 'failed',
                    1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM schedule_fires
                WHERE fire_id = 'fire-1'
                """
            )
    finally:
        engine.dispose()

    with _preserve_logging_state():
        command.downgrade(config, "119_work_scope_revisions")
    schema = _schema(database_path)
    assert "fire_kind" not in schema["columns"]
    assert schema["unique"] == {("schedule_id", "scheduled_fire_at")}
    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT fire_id FROM schedule_fires"
            ).scalars().all() == ["fire-1"]
    finally:
        engine.dispose()
