from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cognis import json_codec
from cognis.core.executor_policy import ExecutorPolicy
from cognis.core.executor_pool import (
    ExecutorAvailability,
    refresh_executor_eligibility,
    resolve_executor_pool,
    tool_observed_on,
)
from cognis.store.models import Base, ExecutorRow, User


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def catalog_db(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], dict[str, int], list[str]]]:
    url = "sqlite+aiosqlite://"
    if request.param == "postgresql":
        configured = os.environ.get("COGNIS_TEST_POOL_POSTGRES_URL")
        if not configured:
            pytest.skip("requires disposable PostgreSQL")
        assert urlsplit(configured).hostname in {"127.0.0.1", "localhost", "::1"}
        url = configured
    decoded = {"catalog_bytes": 0}
    statements: list[str] = []

    def decode(value: str | bytes) -> object:
        result = json_codec.loads(value)
        if (
            isinstance(result, list)
            and result
            and isinstance(result[0], dict)
            and str(result[0].get("name", "")).startswith("catalog_probe_")
        ):
            decoded["catalog_bytes"] += len(value)
        return result

    engine = create_async_engine(url, json_serializer=json_codec.dumps, json_deserializer=decode)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    tables = [User.__table__, ExecutorRow.__table__]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(User(email="owner@test"))
            await session.commit()
        yield factory, decoded, statements
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["explicit", "selector"])
async def test_refresh_keeps_catalog_without_loading_another_copy(
    catalog_db: tuple[async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    selection: str,
) -> None:
    factory, decoded, statements = catalog_db
    catalog = [
        {
            "name": f"catalog_probe_{i}",
            "input_schema": {
                "type": "object",
                "properties": {
                    f"field_{j}": {"type": "string", "description": f"{i}:{j}:" + "x" * 640}
                    for j in range(24)
                },
            },
        }
        for i in range(512)
    ]
    assert len(json_codec.dumps(catalog)) > 8 * 1024 * 1024
    async with factory() as session:
        session.add(
            ExecutorRow(
                executor_id="catalog_primary",
                name="Catalog primary",
                owner_email="owner@test",
                executor_type="websocket",
                labels={"tier": "primary"},
                runtime_state="active",
                enabled_tools=["*"],
                observed_tools=catalog,
            )
        )
        await session.commit()
    execution = (
        {"executor_id": "catalog_primary"}
        if selection == "explicit"
        else {"executor_selector": {"tier": "primary"}}
    )
    args = dict(
        session_factory=factory,
        agent_execution=execution,
        user_email="owner@test",
        executor_owner_email="owner@test",
        policy=ExecutorPolicy(),
    )
    original = await resolve_executor_pool(**args)
    pinned = original.primary[0].observed_tools
    assert original.primary[0].state == ExecutorAvailability.USABLE
    assert decoded["catalog_bytes"] > 8 * 1024 * 1024
    assert tool_observed_on(original.primary[0], "catalog_probe_0")

    async with factory() as session:
        await session.execute(
            update(ExecutorRow)
            .where(ExecutorRow.executor_id == "catalog_primary")
            .values(runtime_state="offline", enabled_tools=["catalog_probe_0"])
        )
        session.add(
            ExecutorRow(
                executor_id="new_candidate",
                name="New candidate",
                owner_email="owner@test",
                executor_type="websocket",
                labels={"tier": "primary"},
                runtime_state="active",
                observed_tools=[{"name": "new_catalog_tool"}],
            )
        )
        await session.commit()
    decoded["catalog_bytes"] = 0
    statements.clear()
    current = original
    for _ in range(3):
        current = await refresh_executor_eligibility(current, **args)
        assert [target.executor_id for target in current.primary] == ["catalog_primary"]
        target = current.primary[0]
        assert target.state == ExecutorAvailability.OFFLINE
        assert target.runtime_state == "offline"
        assert target.enabled_tools == ["catalog_probe_0"]
        assert target.observed_tools is pinned
        assert tool_observed_on(target, "catalog_probe_0", require_usable=False)
    assert decoded["catalog_bytes"] == 0
    assert statements
    assert all("executors.observed_tools" not in sql for sql in statements)
    assert "observed_tools" in inspect(current.primary[0].row).unloaded

    # The refresh must still re-read authorization instead of caching eligibility.
    async with factory() as session:
        await session.execute(
            update(ExecutorRow)
            .where(ExecutorRow.executor_id == "catalog_primary")
            .values(status="disabled")
        )
        await session.commit()
    blocked = await refresh_executor_eligibility(current, **args)
    assert blocked.primary[0].state == ExecutorAvailability.BLOCKED
