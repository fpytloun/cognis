from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from cognis.store.database import DB_POOL_CHECKED_OUT, create_engine, pool_snapshot


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get("COGNIS_TEST_POOL_POSTGRES_URL")
    if not url:
        pytest.skip("requires disposable PostgreSQL with password authentication")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    return url


async def _burst(engine: AsyncEngine, concurrency: int) -> None:
    barrier = asyncio.Barrier(concurrency)

    async def query() -> None:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            await barrier.wait()

    async with asyncio.TaskGroup() as tasks:
        for _ in range(concurrency):
            tasks.create_task(query())


@pytest.mark.asyncio
async def test_repeated_bursts_reuse_authenticated_connections(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    created = 0
    closed = 0

    @event.listens_for(engine.sync_engine, "connect")
    def connected(*_args: object) -> None:
        nonlocal created
        created += 1

    @event.listens_for(engine.sync_engine, "close")
    def disconnected(*_args: object) -> None:
        nonlocal closed
        closed += 1

    gauge_before = DB_POOL_CHECKED_OUT._value.get()
    try:
        # Above the former five-connection retention, below the unchanged cap.
        for _ in range(4):
            await asyncio.wait_for(_burst(engine, 12), timeout=20)
        assert created == 12
        assert closed == 0
        assert pool_snapshot(engine)["checkedin"] == 12
        assert pool_snapshot(engine)["checkedout"] == 0
        assert DB_POOL_CHECKED_OUT._value.get() == gauge_before
    finally:
        await engine.dispose()
    assert closed == created


@pytest.mark.asyncio
async def test_connection_retention_capacity_without_connecting() -> None:
    engine = create_engine("postgresql+asyncpg://localhost/cognis_pool_validation")
    try:
        snapshot = pool_snapshot(engine)
        assert snapshot["size"] == 15
        assert snapshot["checkedin"] == 0
        assert snapshot["checkedout"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_capacity_waiter_cancellation_and_transaction_cleanup(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    connections: list[AsyncConnection] = []
    gauge_before = DB_POOL_CHECKED_OUT._value.get()
    try:
        for _ in range(15):
            connections.append(await engine.connect())
        assert pool_snapshot(engine)["checkedout"] == 15
        assert DB_POOL_CHECKED_OUT._value.get() == gauge_before + 15

        waiter_entered = asyncio.Event()

        async def unexpected_checkout() -> None:
            waiter_entered.set()
            async with engine.connect():
                raise AssertionError("database connection cap exceeded")

        waiter = asyncio.create_task(unexpected_checkout())
        try:
            await waiter_entered.wait()
            assert not waiter.done()
            # Pool accounting includes a slot even while a new connection is
            # authenticating, so this also rejects an in-flight 16th connection.
            assert pool_snapshot(engine)["checkedout"] == 15
        finally:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert pool_snapshot(engine)["checkedout"] == 15

        # Return exactly one connection, ensuring the next checkout reuses the
        # same backend and can inspect its connection-local temporary table.
        returned = connections[-1]
        await returned.execute(text("CREATE TEMP TABLE pool_rollback_probe (value integer)"))
        await returned.commit()
        await returned.execute(text("INSERT INTO pool_rollback_probe VALUES (1)"))
        await returned.close()
        connections.pop()
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM pool_rollback_probe")) == 0
        for connection in connections:
            await connection.close()
        connections.clear()
        assert pool_snapshot(engine)["checkedout"] == 0

        await asyncio.wait_for(_burst(engine, 15), timeout=20)
        assert DB_POOL_CHECKED_OUT._value.get() == gauge_before
    finally:
        for connection in connections:
            await connection.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_retained_connection_is_replaced(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    control = create_engine(postgres_url)
    created = 0
    gauge_before = DB_POOL_CHECKED_OUT._value.get()

    @event.listens_for(engine.sync_engine, "connect")
    def connected(*_args: object) -> None:
        nonlocal created
        created += 1

    try:
        async with engine.connect() as connection:
            old_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(old_pid, int)
        async with control.connect() as connection:
            assert await connection.scalar(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": old_pid}
            )
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT pg_backend_pid()")) != old_pid
            assert await connection.scalar(text("SELECT 1")) == 1
        assert created == 2
        assert pool_snapshot(engine)["checkedout"] == 0
        assert DB_POOL_CHECKED_OUT._value.get() == gauge_before
    finally:
        await engine.dispose()
        await control.dispose()
