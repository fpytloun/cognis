from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from cognis.bootstrap import (
    _ensure_session_auth_versions,
    _ensure_user_auth_version,
    run_schema_bootstrap,
)
from cognis.store.database import create_engine


@pytest.mark.asyncio
async def test_schema_bootstrap_creates_mfa_tables_idempotently(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'mfa-schema.db'}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            (
                tables,
                user_columns,
                browser_columns,
                native_columns,
                challenge_columns,
                challenge_indexes,
                recovery_uniques,
            ) = await connection.run_sync(
                lambda conn: (
                    set(inspect(conn).get_table_names()),
                    inspect(conn).get_columns("users"),
                    inspect(conn).get_columns("browser_sessions"),
                    inspect(conn).get_columns("native_sessions"),
                    inspect(conn).get_columns("mfa_challenges"),
                    inspect(conn).get_indexes("mfa_challenges"),
                    inspect(conn).get_unique_constraints("mfa_recovery_codes"),
                )
            )
    finally:
        await engine.dispose()
    assert {
        "user_totp_factors",
        "mfa_challenges",
        "mfa_recovery_codes",
        "mfa_attempt_budgets",
    } <= tables
    auth_version = next(column for column in user_columns if column["name"] == "auth_version")
    assert auth_version["nullable"] is False
    for columns in (browser_columns, native_columns, challenge_columns):
        bound_version = next(column for column in columns if column["name"] == "auth_version")
        assert bound_version["nullable"] is False
    assert {index["name"] for index in challenge_indexes} == {
        "ix_mfa_challenges_expires_at",
        "ix_mfa_challenges_user_email",
    }
    assert {item["name"] for item in recovery_uniques} == {"uq_mfa_recovery_codes_code_hash"}


@pytest.mark.asyncio
async def test_user_auth_version_bootstrap_is_idempotent_for_existing_users_table(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth-version.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE users (email VARCHAR PRIMARY KEY)"))
            await connection.run_sync(_ensure_user_auth_version)
            await connection.run_sync(_ensure_user_auth_version)
            columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("users"))
    finally:
        await engine.dispose()
    auth_version = next(column for column in columns if column["name"] == "auth_version")
    assert auth_version["nullable"] is False


@pytest.mark.asyncio
async def test_session_auth_version_bootstrap_upgrades_previous_schema_twice(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'session-auth-version.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE browser_sessions "
                    "(session_id VARCHAR PRIMARY KEY, token_hash VARCHAR NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE native_sessions "
                    "(session_id VARCHAR PRIMARY KEY, token_hash VARCHAR NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE mfa_challenges "
                    "(challenge_id VARCHAR PRIMARY KEY, token_hash VARCHAR NOT NULL)"
                )
            )
            await connection.run_sync(_ensure_session_auth_versions)
            await connection.run_sync(_ensure_session_auth_versions)
            columns = await connection.run_sync(
                lambda conn: {
                    table_name: inspect(conn).get_columns(table_name)
                    for table_name in (
                        "browser_sessions",
                        "native_sessions",
                        "mfa_challenges",
                    )
                }
            )
    finally:
        await engine.dispose()
    for table_columns in columns.values():
        auth_version = next(column for column in table_columns if column["name"] == "auth_version")
        assert auth_version["nullable"] is False
