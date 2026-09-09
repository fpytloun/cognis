from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from cognis.bootstrap import run_schema_bootstrap
from cognis.store.database import create_engine


@pytest.mark.asyncio
async def test_schema_bootstrap_creates_native_sessions_idempotently(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'native-sessions.db'}")
    try:
        await run_schema_bootstrap(engine)
        await run_schema_bootstrap(engine)
        async with engine.connect() as connection:
            tables, columns, indexes, unique_constraints = await connection.run_sync(
                lambda conn: (
                    inspect(conn).get_table_names(),
                    inspect(conn).get_columns("native_sessions"),
                    inspect(conn).get_indexes("native_sessions"),
                    inspect(conn).get_unique_constraints("native_sessions"),
                )
            )
    finally:
        await engine.dispose()

    assert "native_sessions" in tables
    assert "family_id" in {column["name"] for column in columns}
    assert {index["name"] for index in indexes} == {
        "ix_native_sessions_expires_at",
        "ix_native_sessions_family_id",
        "ix_native_sessions_user_email",
    }
    assert {constraint["name"] for constraint in unique_constraints} == {
        "uq_native_sessions_token_hash"
    }
