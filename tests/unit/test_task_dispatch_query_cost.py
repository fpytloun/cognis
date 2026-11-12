from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, insert, literal, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cognis.core.invariants.task_dispatches import _cancelled_dispatches
from cognis.store.models import Base, Task
from cognis.store.queries import create_agent, create_user


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def engine(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    admin = None
    schema = None
    if request.param == "postgresql":
        url = os.getenv("COGNIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("COGNIS_TEST_POSTGRES_URL is not configured")
        schema = f"dispatch_query_{uuid4().hex}"
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        value = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        value = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    try:
        async with value.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield value
    finally:
        await value.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def _seed(engine: AsyncEngine, values: list[dict[str, Any]]) -> list[str]:
    owner, agent_id = f"{uuid4().hex}@example.org", uuid4().hex
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = [f"{i:08d}-{uuid4().hex}" for i in range(len(values))]
    async with factory() as session:
        await create_user(session, email=owner, name="Query test", password_hash="hash")
        await create_agent(session, agent_id=agent_id, name="Query test", owner_email=owner)
        await session.execute(
            insert(Task),
            [
                {
                    "task_id": task_id,
                    "title": "Synthetic task",
                    "created_by": owner,
                    "agent_id": agent_id,
                    **value,
                }
                for task_id, value in zip(ids, values, strict=True)
            ],
        )
        await session.commit()
    return ids


@pytest.mark.asyncio
async def test_recovery_filter_preserves_null_empty_and_status_semantics(
    engine: AsyncEngine,
) -> None:
    recovery = {"execution_recovery": {"reason": "pending_dispatch"}}
    ids = await _seed(
        engine,
        [
            {"status": "cancelled", "result_data": None},
            {"status": "cancelled", "result_data": {}},
            {"status": "cancelled", "result_data": {"execution_recovery": None}},
            {"status": "cancelled", "result_data": {"execution_recovery": {}}},
            {"status": "cancelled", "result_data": {"execution_recovery": "{}"}},
            {"status": "completed", "result_data": recovery},
            {"status": "running", "result_data": recovery},
            {"status": "cancelled", "result_data": recovery},
        ],
    )
    # Exercise SQL NULL as well as the JSON null produced by normal JSON inserts.
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE tasks SET result_data=NULL WHERE task_id=:task_id"), {"task_id": ids[0]}
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    snapshots = [row async for row in _cancelled_dispatches(factory, None)]
    assert [row[0] for row in snapshots] == [ids[-1]]
    assert snapshots[0][2] == recovery["execution_recovery"]
    assert [row async for row in _cancelled_dispatches(factory, ids[5])] == []
    assert [row[0] async for row in _cancelled_dispatches(factory, ids[-1])] == [ids[-1]]


@pytest.mark.asyncio
async def test_recovery_filter_keeps_keyset_pagination(engine: AsyncEngine) -> None:
    ids = await _seed(
        engine,
        [
            {"status": "cancelled", "result_data": {"execution_recovery": {"index": i}}}
            for i in range(71)
        ],
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    assert [row[0] async for row in _cancelled_dispatches(factory, None)] == ids


@pytest.mark.asyncio
async def test_generic_postgresql_poll_does_not_read_unrelated_toasted_json(
    engine: AsyncEngine, record_property: Any
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL generic prepared plan and TOAST")
    padding = hashlib.shake_256(b"synthetic-task-history").hexdigest(4096)
    await _seed(
        engine,
        [
            {"status": "cancelled", "result_data": None}
            if i % 40 == 0
            else {"status": "completed", "result_data": {"padding": padding}}
            for i in range(5440)
        ],
    )
    captured: list[tuple[str, tuple[Any, ...]]] = []

    def capture(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if statement.startswith("SELECT tasks.") and "ORDER BY tasks.task_id" in statement:
            captured.append((statement, tuple(parameters)))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        assert [row async for row in _cancelled_dispatches(factory, None)] == []
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(captured) == 1
    statement, parameters = captured[0]
    arguments = ",".join(
        str(
            literal(value).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        for value in parameters
    )
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("ANALYZE tasks"))
        await connection.execute(text("SET plan_cache_mode=force_generic_plan"))
        await connection.execute(
            text(
                "PREPARE original_dispatch_poll(varchar,text,text,varchar,integer) AS "
                "SELECT * FROM tasks WHERE status=$1 AND "
                "CAST(result_data ->> $2 AS VARCHAR) IS NOT NULL AND "
                "CAST(result_data ->> $3 AS VARCHAR) != $4 ORDER BY task_id LIMIT $5"
            )
        )
        await connection.execute(text(f"PREPARE fenced_dispatch_poll AS {statement}"))
        prefix = "EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) EXECUTE "
        original = (
            await connection.scalar(
                text(
                    prefix + "original_dispatch_poll("
                    "'cancelled','execution_recovery','execution_recovery','{}',50)"
                )
            )
        )[0]
        fenced = (await connection.scalar(text(prefix + f"fenced_dispatch_poll({arguments})")))[0]
        await connection.execute(text("DEALLOCATE original_dispatch_poll"))
        await connection.execute(text("DEALLOCATE fenced_dispatch_poll"))
        await connection.execute(text("RESET plan_cache_mode"))
    assert original["Plan"]["Actual Rows"] == fenced["Plan"]["Actual Rows"] == 0
    assert fenced["Plan"]["Shared Hit Blocks"] < original["Plan"]["Shared Hit Blocks"] / 4
    record_property(
        "dispatch_query_cost",
        {
            "original_execution_ms": original["Execution Time"],
            "fenced_execution_ms": fenced["Execution Time"],
            "original_buffer_hits": original["Plan"]["Shared Hit Blocks"],
            "fenced_buffer_hits": fenced["Plan"]["Shared Hit Blocks"],
        },
    )
