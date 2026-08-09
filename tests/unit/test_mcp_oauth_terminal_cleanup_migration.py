from __future__ import annotations

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
    import logging

    handlers = list(logging.root.handlers)
    level = logging.root.level
    try:
        yield
    finally:
        logging.root.handlers[:] = handlers
        logging.root.setLevel(level)


def _terminal_cleanup_schema(
    path: Path,
) -> tuple[dict[str, tuple[bool, str | None]], set[str]]:
    engine = create_sync_engine(f"sqlite:///{path}")
    try:
        inspector = inspect(engine)
        columns = {
            column["name"]: (
                bool(column["nullable"]),
                str(column["default"]) if column["default"] is not None else None,
            )
            for column in inspector.get_columns("mcp_oauth_transactions")
        }
        indexes = {index["name"] for index in inspector.get_indexes("mcp_oauth_transactions")}
        return columns, indexes
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_mcp_oauth_terminal_cleanup_bootstrap_matches_migration(
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
        command.upgrade(config, "101_mcp_oauth_cleanup_dispatch")

    assert _terminal_cleanup_schema(bootstrap_path) == _terminal_cleanup_schema(migration_path)
    columns, indexes = _terminal_cleanup_schema(bootstrap_path)
    assert {
        "terminal_cleanup_required",
        "terminal_notification_resolved_at",
        "terminal_reconfigure_applied_at",
        "terminal_reconfigure_completed_at",
    } <= set(columns)
    cleanup_nullable, cleanup_default = columns["terminal_cleanup_required"]
    assert cleanup_nullable is False
    assert cleanup_default is not None
    assert cleanup_default.lower().strip("()'\"") in {"0", "false"}
    assert "ix_mcp_oauth_transactions_terminal_cleanup" in indexes

    with _preserve_logging_state():
        command.downgrade(config, "100_mcp_oauth_terminal_cleanup")
    downgraded_columns, downgraded_indexes = _terminal_cleanup_schema(migration_path)
    assert "terminal_reconfigure_completed_at" not in downgraded_columns
    assert "ix_mcp_oauth_transactions_terminal_cleanup" in downgraded_indexes
