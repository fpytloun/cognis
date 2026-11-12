from __future__ import annotations

import json
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.usage_query_schema_fixtures import exercise_query_upgrade, explain_query_indexes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("COGNIS_TEST_POSTGRES_URL"), reason="PostgreSQL is not configured"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upgrade", "explain"])
async def test_query_contract_postgresql(operation: str) -> None:
    url = os.environ["COGNIS_TEST_POSTGRES_URL"]
    schema = f"cognis_usage_query_{uuid.uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(sa.schema.CreateSchema(schema))
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": f'"{schema}"'}},
    )
    try:
        async with engine.begin() as connection:
            if operation == "upgrade":
                await connection.run_sync(exercise_query_upgrade)
            else:
                evidence = await connection.run_sync(explain_query_indexes)
                print("usage_query_index_plans=" + json.dumps(evidence))
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(sa.schema.DropSchema(schema, cascade=True))
        await admin.dispose()
