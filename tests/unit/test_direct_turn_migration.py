from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, TIMESTAMP, BigInteger, Integer, String, inspect
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.engine import Engine

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine as create_async_engine
from cognis.store.models import DirectTurnRequestRow


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


def _normalized_type(column_type: object) -> str:
    if isinstance(column_type, BigInteger):
        return "bigint"
    if isinstance(column_type, Integer):
        return "integer"
    if isinstance(column_type, JSON):
        return "json"
    if isinstance(column_type, TIMESTAMP):
        return "timestamptz" if column_type.timezone else "timestamp"
    if isinstance(column_type, String):
        return "string"
    return str(column_type).lower()


def _normalized_default(default: object) -> str | None:
    if default is None:
        return None
    return " ".join(str(default).strip().lower().split())


def _direct_turn_schema(engine: Engine) -> dict[str, Any]:
    inspector = inspect(engine)
    columns = {
        column["name"]: {
            "nullable": column["nullable"],
            "primary_key": column["primary_key"],
            "type": _normalized_type(column["type"]),
            "default": _normalized_default(column["default"]),
        }
        for column in inspector.get_columns("direct_turn_requests")
    }
    return {
        "columns": columns,
        "primary_key": tuple(
            inspector.get_pk_constraint("direct_turn_requests")["constrained_columns"]
        ),
        "indexes": {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("direct_turn_requests")
        },
        "unique_columns": {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("direct_turn_requests")
        },
        "foreign_keys": {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                foreign_key["options"].get("ondelete"),
            )
            for foreign_key in inspector.get_foreign_keys("direct_turn_requests")
        },
        "checks": {
            constraint["name"]
            for constraint in inspector.get_check_constraints("direct_turn_requests")
        },
    }


def _assert_direct_turn_schema(schema: dict[str, Any]) -> None:
    expected_columns = set(DirectTurnRequestRow.__table__.columns.keys())
    assert set(schema["columns"]) == expected_columns
    assert schema["primary_key"] == ("admission_order",)
    assert schema["columns"]["admission_order"] == {
        "nullable": False,
        "primary_key": 1,
        "type": "integer",
        "default": None,
    }
    for required in (
        "request_id",
        "turn_id",
        "conversation_id",
        "agent_id",
        "user_id",
        "idempotency_scope",
        "idempotency_key",
        "admission_hash",
        "payload_hash",
        "payload_version",
        "payload",
        "status",
        "attempt_count",
        "created_at",
        "updated_at",
    ):
        assert schema["columns"][required]["nullable"] is False
    expected_types = {
        "admission_order": "integer",
        "fencing_token": "bigint",
        "payload_version": "integer",
        "attempt_count": "integer",
        "payload": "json",
        "outcome": "json",
        "created_at": "timestamp",
        "updated_at": "timestamp",
        "claimed_at": "timestamp",
        "started_at": "timestamp",
        "terminal_at": "timestamp",
        "cancel_requested_at": "timestamp",
        "next_attempt_at": "timestamp",
    }
    for name, expected_type in expected_types.items():
        assert schema["columns"][name]["type"] == expected_type
    assert all(column["default"] is None for column in schema["columns"].values())
    assert schema["indexes"] == {
        "ix_direct_turn_requests_fifo": (
            "conversation_id",
            "status",
            "admission_order",
        ),
        "ix_direct_turn_requests_owner": (
            "owner_controller_id",
            "owner_incarnation_id",
            "status",
        ),
        "ix_direct_turn_requests_status_due": (
            "status",
            "next_attempt_at",
        ),
    }
    assert schema["unique_columns"] == {
        ("idempotency_scope", "idempotency_key"),
        ("request_id",),
        ("turn_id",),
    }
    assert schema["foreign_keys"] == {
        (("conversation_id",), "conversations", ("conversation_id",), "CASCADE"),
        (("user_id",), "users", ("email",), "CASCADE"),
    }
    assert schema["checks"] == {"ck_direct_turn_requests_status"}


def test_direct_turn_migration_upgrade_and_downgrade_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "direct-turn-migration.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")

    with _preserve_logging_state():
        command.upgrade(config, "123_direct_turn_retry_schedule")

    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        assert "direct_turn_requests" in inspector.get_table_names()
        _assert_direct_turn_schema(_direct_turn_schema(engine))
    finally:
        engine.dispose()

    with _preserve_logging_state():
        command.downgrade(config, "096_coordination_leases")

    engine = create_sync_engine(f"sqlite:///{database_path}")
    try:
        assert "direct_turn_requests" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_direct_turn_bootstrap_matches_migration_schema(tmp_path: Path) -> None:
    bootstrap_path = tmp_path / "direct-turn-bootstrap.db"
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{bootstrap_path}")
    await run_schema_bootstrap(async_engine)
    await run_schema_bootstrap(async_engine)
    await async_engine.dispose()

    bootstrap_engine = create_sync_engine(f"sqlite:///{bootstrap_path}")
    migration_path = tmp_path / "direct-turn-migration-parity.db"
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{migration_path}")
    with _preserve_logging_state():
        command.upgrade(config, "123_direct_turn_retry_schedule")
    migration_engine = create_sync_engine(f"sqlite:///{migration_path}")
    try:
        bootstrap_schema = _direct_turn_schema(bootstrap_engine)
        migration_schema = _direct_turn_schema(migration_engine)
        _assert_direct_turn_schema(bootstrap_schema)
        assert bootstrap_schema == migration_schema
    finally:
        bootstrap_engine.dispose()
        migration_engine.dispose()
