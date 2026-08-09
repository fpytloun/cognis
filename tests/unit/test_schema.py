from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from cognis.store.database import create_engine
from cognis.store.schema import expected_schema_heads, validate_schema


@pytest.mark.asyncio
async def test_validate_schema_is_read_only_for_empty_database(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")

    status = await validate_schema(engine)

    assert status.compatible is False
    async with engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync_connection: set(
                __import__("sqlalchemy").inspect(sync_connection).get_table_names()
            )
        )
    assert tables == set()
    await engine.dispose()


@pytest.mark.asyncio
async def test_validate_schema_accepts_expected_head(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'versioned.db'}")
    expected = expected_schema_heads()
    assert len(expected) == 1
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": next(iter(expected))},
        )

    status = await validate_schema(engine, expected_heads=expected)

    assert status.compatible is True
    await engine.dispose()
