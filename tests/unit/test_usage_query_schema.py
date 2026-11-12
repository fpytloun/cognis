from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.usage_query_schema_fixtures import exercise_query_upgrade


@pytest.mark.asyncio
async def test_query_contract_previous_180_sqlite(tmp_path: Any) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'previous180.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(exercise_query_upgrade)
    finally:
        await engine.dispose()
