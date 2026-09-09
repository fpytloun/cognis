from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql

from cognis.bootstrap import (
    _ensure_work_call_state_index,
    _ensure_work_load_indexes,
    run_schema_bootstrap,
)
from cognis.store.database import create_engine as create_async_engine
from cognis.store.models import DirectTurnRequestRow, Session, StepRun

EXPECTED_INDEXES = {
    "sessions": {
        "ix_sessions_owner_conversation_activity_scope_session": (
            "user_email",
            "conversation_id",
            "activity_scope_id",
            "session_id",
        ),
    },
    "step_runs": {
        "ix_step_runs_task_step_run": ("task_id", "step_run_id"),
        "ix_step_runs_session_status": ("session_id", "status"),
    },
}
DIRECT_TURN_INDEX = (
    "ix_direct_turn_requests_session_latest",
    ("user_id", "session_id", "admission_order"),
)
ACTIVITY_LIST_INDEX = (
    "ix_sessions_owner_activity_scope_updated",
    ("user_email", "activity_scope_id", "updated_at", "session_id"),
)


class _CatalogResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _CatalogResult:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


def _postgresql_index_row(name: str, *, valid: bool = True) -> dict[str, Any]:
    bootstrap_indexes = {
        **EXPECTED_INDEXES,
        "direct_turn_requests": {DIRECT_TURN_INDEX[0]: DIRECT_TURN_INDEX[1]},
    }
    table, columns = next(
        (table, columns)
        for table, indexes in bootstrap_indexes.items()
        for index_name, columns in indexes.items()
        if index_name == name
    )
    return {
        "valid": valid,
        "table_name": table,
        "is_unique": False,
        "has_no_predicate": True,
        "has_no_expressions": True,
        "access_method": "btree",
        "key_count": len(columns),
        "total_count": len(columns),
        "columns": columns,
        "definition": f"CREATE INDEX {name} ON public.{table} USING btree ({', '.join(columns)})",
    }


def _activity_list_postgresql_index_row() -> dict[str, Any]:
    return {
        **_postgresql_index_row("ix_sessions_owner_conversation_activity_scope_session"),
        "columns": ACTIVITY_LIST_INDEX[1],
        "definition": "CREATE INDEX ix_sessions_owner_activity_scope_updated "
        "ON public.sessions USING btree "
        "(user_email, activity_scope_id, updated_at, session_id)",
    }


def _all_postgresql_index_rows() -> dict[str, dict[str, Any]]:
    rows = {
        name: _postgresql_index_row(name)
        for indexes in EXPECTED_INDEXES.values()
        for name in indexes
    }
    rows[ACTIVITY_LIST_INDEX[0]] = _activity_list_postgresql_index_row()
    rows[DIRECT_TURN_INDEX[0]] = _postgresql_index_row(DIRECT_TURN_INDEX[0])
    return rows


def _postgresql_connection(
    rows: dict[str, dict[str, Any] | None],
) -> tuple[SimpleNamespace, list[str], list[str]]:
    statements: list[str] = []
    catalog_queries: list[str] = []

    def execute(statement: object, parameters: dict[str, str] | None = None) -> object:
        sql = str(statement)
        if parameters is not None:
            catalog_queries.append(sql)
            return _CatalogResult(rows.get(parameters["name"]))
        statements.append(sql)
        return MagicMock()

    connection = SimpleNamespace(dialect=postgresql.dialect(), execute=execute)
    return connection, statements, catalog_queries


def _config(database_path: Path) -> Config:
    config = Config("cognis/store/migrations/alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    config.config_file_name = None
    return config


def _indexes(database_path: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(engine)
        return {
            table: {
                str(index["name"]): tuple(index["column_names"])
                for index in inspector.get_indexes(table)
            }
            for table in EXPECTED_INDEXES
        }
    finally:
        engine.dispose()


def test_model_metadata_declares_exact_work_load_indexes() -> None:
    session_indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in Session.__table__.indexes
    }
    step_indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in StepRun.__table__.indexes
    }
    direct_turn_indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in DirectTurnRequestRow.__table__.indexes
    }

    for name, columns in EXPECTED_INDEXES["sessions"].items():
        assert session_indexes[name] == columns
    assert session_indexes[ACTIVITY_LIST_INDEX[0]] == ACTIVITY_LIST_INDEX[1]
    for name, columns in EXPECTED_INDEXES["step_runs"].items():
        assert step_indexes[name] == columns
    assert direct_turn_indexes[DIRECT_TURN_INDEX[0]] == DIRECT_TURN_INDEX[1]


def test_work_load_index_migration_upgrades_and_downgrades_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "work-load-indexes.db"
    config = _config(database_path)
    command.upgrade(config, "127_work_record_categories")

    command.upgrade(config, "128_work_load_indexes")
    upgraded = _indexes(database_path)
    for table, expected in EXPECTED_INDEXES.items():
        for name, columns in expected.items():
            assert upgraded[table][name] == columns
    command.downgrade(config, "127_work_record_categories")
    downgraded = _indexes(database_path)
    for table, expected in EXPECTED_INDEXES.items():
        assert expected.keys().isdisjoint(downgraded[table])


def test_work_load_index_migration_accepts_preexisting_indexes(tmp_path: Path) -> None:
    database_path = tmp_path / "preexisting-work-load-indexes.db"
    config = _config(database_path)
    command.upgrade(config, "127_work_record_categories")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            for name, table_indexes in EXPECTED_INDEXES.items():
                for index_name, columns in table_indexes.items():
                    connection.execute(
                        text(f"CREATE INDEX {index_name} ON {name} ({', '.join(columns)})")
                    )
    finally:
        engine.dispose()

    command.upgrade(config, "128_work_load_indexes")
    upgraded = _indexes(database_path)
    for table, expected in EXPECTED_INDEXES.items():
        for name, columns in expected.items():
            assert upgraded[table][name] == columns


def test_activity_list_index_migration_upgrades_and_downgrades_sqlite(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "work-activity-list-index.db"
    config = _config(database_path)
    command.upgrade(config, "133_work_generation_snapshots")

    command.upgrade(config, "134_work_activity_list_index")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        indexes = {
            str(index["name"]): tuple(index["column_names"])
            for index in inspect(engine).get_indexes("sessions")
        }
        assert indexes[ACTIVITY_LIST_INDEX[0]] == ACTIVITY_LIST_INDEX[1]
    finally:
        engine.dispose()

    command.downgrade(config, "133_work_generation_snapshots")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        names = {str(index["name"]) for index in inspect(engine).get_indexes("sessions")}
        assert ACTIVITY_LIST_INDEX[0] not in names
    finally:
        engine.dispose()


def test_activity_list_index_migration_repairs_conflicting_sqlite_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "work-activity-list-conflict.db"
    config = _config(database_path)
    command.upgrade(config, "133_work_generation_snapshots")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX ix_sessions_owner_activity_scope_updated "
                    "ON sessions (user_email, session_id)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "134_work_activity_list_index")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        indexes = {
            str(index["name"]): tuple(index["column_names"])
            for index in inspect(engine).get_indexes("sessions")
        }
        assert indexes[ACTIVITY_LIST_INDEX[0]] == ACTIVITY_LIST_INDEX[1]
    finally:
        engine.dispose()


def test_activity_list_index_migration_repairs_partial_sqlite_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "work-activity-list-partial.db"
    config = _config(database_path)
    command.upgrade(config, "133_work_generation_snapshots")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX ix_sessions_owner_activity_scope_updated "
                    "ON sessions (user_email, activity_scope_id, updated_at, session_id) "
                    "WHERE status = 'active'"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "134_work_activity_list_index")
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        indexes = {str(index["name"]): index for index in inspect(engine).get_indexes("sessions")}
        repaired = indexes[ACTIVITY_LIST_INDEX[0]]
        assert tuple(repaired["column_names"]) == ACTIVITY_LIST_INDEX[1]
        assert (repaired.get("dialect_options") or {}).get("sqlite_where") is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_work_load_index_bootstrap_upgrades_existing_schema_idempotently(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bootstrap-work-load-indexes.db"
    config = _config(database_path)
    command.upgrade(config, "127_work_record_categories")
    before = _indexes(database_path)
    for table, expected in EXPECTED_INDEXES.items():
        assert expected.keys().isdisjoint(before[table])

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
    finally:
        await engine.dispose()

    upgraded = _indexes(database_path)
    for table, expected in EXPECTED_INDEXES.items():
        for name, columns in expected.items():
            assert upgraded[table][name] == columns
    sqlite_engine = create_engine(f"sqlite:///{database_path}")
    try:
        direct_turn_indexes = {
            str(index["name"]): tuple(index["column_names"])
            for index in inspect(sqlite_engine).get_indexes("direct_turn_requests")
        }
        assert direct_turn_indexes[DIRECT_TURN_INDEX[0]] == DIRECT_TURN_INDEX[1]
    finally:
        sqlite_engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_bootstrap_uses_separate_autocommit_connection() -> None:
    transactional_connection = AsyncMock()
    concurrent_connection = AsyncMock()
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.begin.return_value.__aenter__.return_value = transactional_connection
    engine.connect.return_value.__aenter__.return_value = concurrent_connection

    await run_schema_bootstrap(engine)

    transactional_helpers = {
        call.args[0] for call in transactional_connection.run_sync.await_args_list
    }
    assert _ensure_work_load_indexes not in transactional_helpers
    concurrent_connection.execution_options.assert_awaited_once_with(isolation_level="AUTOCOMMIT")
    assert [call.args[0] for call in concurrent_connection.run_sync.await_args_list] == [
        _ensure_work_call_state_index,
        _ensure_work_load_indexes,
    ]


def test_postgresql_bootstrap_creates_work_load_indexes_concurrently() -> None:
    connection, statements, catalog_queries = _postgresql_connection({})

    _ensure_work_load_indexes(connection)

    assert len(catalog_queries) == 5
    assert all("index_metadata.indisvalid AS valid" in query for query in catalog_queries)
    assert statements == [
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_sessions_owner_conversation_activity_scope_session ON sessions "
        "(user_email, conversation_id, activity_scope_id, session_id)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sessions_owner_activity_scope_updated "
        "ON sessions (user_email, activity_scope_id, updated_at, session_id)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_step_runs_task_step_run "
        "ON step_runs (task_id, step_run_id)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_step_runs_session_status "
        "ON step_runs (session_id, status)",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_direct_turn_requests_session_latest "
        "ON direct_turn_requests (user_id, session_id, admission_order)",
    ]


def test_postgresql_bootstrap_accepts_valid_exact_work_load_indexes() -> None:
    rows = _all_postgresql_index_rows()
    connection, statements, catalog_queries = _postgresql_connection(rows)

    _ensure_work_load_indexes(connection)

    assert len(catalog_queries) == 5
    assert statements == []


def test_postgresql_bootstrap_repairs_invalid_work_load_index_concurrently() -> None:
    rows = _all_postgresql_index_rows()
    invalid_name = "ix_sessions_owner_conversation_activity_scope_session"
    rows[invalid_name].update(
        valid=False,
        columns=("session_id",),
        definition="CREATE INDEX invalid",
    )
    connection, statements, _catalog_queries = _postgresql_connection(rows)

    _ensure_work_load_indexes(connection)

    assert statements == [
        "DROP INDEX CONCURRENTLY IF EXISTS ix_sessions_owner_conversation_activity_scope_session",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_sessions_owner_conversation_activity_scope_session ON sessions "
        "(user_email, conversation_id, activity_scope_id, session_id)",
    ]


def test_postgresql_bootstrap_rejects_conflicting_valid_work_load_index() -> None:
    name = "ix_sessions_owner_conversation_activity_scope_session"
    conflicting = _postgresql_index_row(name)
    conflicting["columns"] = ("user_email", "session_id")
    connection, statements, _catalog_queries = _postgresql_connection({name: conflicting})

    with pytest.raises(RuntimeError, match="Conflicting index definition"):
        _ensure_work_load_indexes(connection)

    assert statements == []


def test_postgresql_migration_uses_concurrent_idempotent_ddl() -> None:
    migration = Path("cognis/store/migrations/versions/128_work_load_indexes.py").read_text()

    assert "autocommit_block()" in migration
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in migration
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in migration
