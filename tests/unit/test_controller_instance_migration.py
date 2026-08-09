from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine as create_async_engine


@contextmanager
def _preserve_logging_state() -> Iterator[None]:
    import logging

    handlers = list(logging.root.handlers)
    level = logging.root.level
    try:
        yield
    finally:
        logging.root.handlers[:] = handlers
        logging.root.setLevel(level)


def _schema(path: Path) -> dict[str, Any]:
    engine = create_sync_engine(f"sqlite:///{path}")
    try:
        inspector = inspect(engine)
        return {
            "columns": {
                column["name"]: (column["nullable"], str(column["type"]))
                for column in inspector.get_columns("controller_instances")
            },
            "pk": tuple(inspector.get_pk_constraint("controller_instances")["constrained_columns"]),
            "indexes": {
                index["name"]: tuple(index["column_names"])
                for index in inspector.get_indexes("controller_instances")
            },
            "checks": {
                item["name"] for item in inspector.get_check_constraints("controller_instances")
            },
        }
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_controller_instance_bootstrap_matches_migration(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "bootstrap.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{bootstrap_path}")
    await run_schema_bootstrap(engine)
    await run_schema_bootstrap(engine)
    await engine.dispose()

    migration_path = tmp_path / "migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{migration_path}")
    with _preserve_logging_state():
        command.upgrade(config, "099_controller_instances")

    assert _schema(bootstrap_path) == _schema(migration_path)
    schema = _schema(bootstrap_path)
    assert schema["pk"] == ("owner_id",)
    assert schema["indexes"] == {
        "ix_controller_instances_controller": ("controller_id",),
        "ix_controller_instances_expires": ("expires_at",),
    }
    assert schema["checks"] == {"ck_controller_instances_lifecycle_state"}

    with _preserve_logging_state():
        command.downgrade(config, "098_schedule_fires")
    downgraded_engine = create_sync_engine(f"sqlite:///{migration_path}")
    try:
        assert "controller_instances" not in inspect(downgraded_engine).get_table_names()
    finally:
        downgraded_engine.dispose()
