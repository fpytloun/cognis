import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.maintenance_lease import run_periodic_maintenance
from cognis.store.coordination import DatabaseLeaseStore
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base


@pytest.mark.asyncio
async def test_two_controllers_share_owner_and_handover(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/leases.db")
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    leases = DatabaseLeaseStore(create_session_factory(engine))
    stops = [asyncio.Event(), asyncio.Event()]
    called = [asyncio.Event(), asyncio.Event()]
    calls = [0, 0]

    async def operation(index):
        calls[index] += 1
        called[index].set()

    tasks = [
        asyncio.create_task(
            run_periodic_maintenance(
                lambda i=i: operation(i),
                stop=stops[i],
                interval_seconds=0.02,
                resource_key="maintenance:test",
                lease_store=leases,
            )
        )
        for i in range(2)
    ]
    try:
        async with asyncio.timeout(3):
            while not any(calls):
                await asyncio.sleep(0.01)
        owner = 0 if calls[0] else 1
        await asyncio.sleep(0.1)
        assert calls[1 - owner] == 0
        stops[owner].set()
        await asyncio.wait_for(tasks[owner], 2)
        await asyncio.wait_for(called[1 - owner].wait(), 2)
    finally:
        for stop in stops:
            stop.set()
        await asyncio.gather(*tasks)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("database unavailable")])
async def test_lease_loss_cancels_active_work_and_releases_fenced(monkeypatch, failure):
    monkeypatch.setattr("cognis.core.maintenance_lease._TTL_SECONDS", 0.06)
    token = object()
    stop = asyncio.Event()
    cancelled = asyncio.Event()
    leases = AsyncMock()
    leases.acquire.side_effect = [token, None]
    leases.renew.return_value = None
    leases.renew.side_effect = failure

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            stop.set()

    await asyncio.wait_for(
        run_periodic_maintenance(
            operation,
            stop=stop,
            interval_seconds=1,
            resource_key="maintenance:test",
            lease_store=leases,
        ),
        1,
    )
    assert cancelled.is_set()
    leases.release.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_cancellation_awaits_work_cleanup_and_releases():
    stop = asyncio.Event()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    token = object()
    leases = AsyncMock()
    leases.acquire.return_value = token

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        run_periodic_maintenance(
            operation,
            stop=stop,
            interval_seconds=60,
            resource_key="maintenance:test",
            lease_store=leases,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    leases.release.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_shutdown_during_lease_loss_settles_sqlite_close_before_release(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("cognis.core.maintenance_lease._TTL_SECONDS", 0.06)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/cleanup.db")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE cleanup_probe (id INTEGER PRIMARY KEY)"))
    closing = asyncio.Event()
    allow_close = asyncio.Event()
    closed = asyncio.Event()
    writing = asyncio.Event()
    stop = asyncio.Event()

    class ClosingSession(AsyncSession):
        async def close(self) -> None:
            closing.set()
            stop.set()
            await allow_close.wait()
            await super().close()
            closed.set()

    sessions = async_sessionmaker(engine, class_=ClosingSession)
    token = object()
    leases = AsyncMock()
    leases.acquire.return_value = token
    release_states: list[bool] = []

    async def lose_lease(*args, **kwargs):
        await writing.wait()
        return None

    async def release_lease(lease):
        release_states.append(closed.is_set())

    leases.renew.side_effect = lose_lease
    leases.release.side_effect = release_lease

    async def operation():
        async with sessions() as session:
            await session.execute(text("INSERT INTO cleanup_probe (id) VALUES (1)"))
            writing.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        run_periodic_maintenance(
            operation,
            stop=stop,
            interval_seconds=60,
            resource_key="maintenance:close-probe",
            lease_store=leases,
        )
    )
    try:
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert release_states == [True]
        leases.release.assert_awaited_once_with(token)
        async with engine.connect() as connection:
            await connection.execute(text("BEGIN IMMEDIATE"))
            await connection.rollback()
    finally:
        allow_close.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 1)
        await engine.dispose()
