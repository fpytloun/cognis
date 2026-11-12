from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.unit.test_usage_schema import exercise_usage_schema

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"),
        reason="COGNIS_TEST_POSTGRES_URL is not configured",
    ),
]


@pytest.mark.asyncio
async def test_usage_postgresql_schema_upgrade_preservation_and_downgrade() -> None:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    schema = f"cognis_usage_schema_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema}"'}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(exercise_usage_schema)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema, cascade=True))
        await admin.dispose()
