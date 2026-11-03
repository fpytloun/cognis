"""Database-session cleanup remains owned after caller cancellation."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.core.shutdown import ShutdownCallbacks, ShutdownCoordinator
from cognis.store.database import create_engine, create_session_factory


@pytest_asyncio.fixture(
    params=["sqlite", *(["postgresql"] if os.getenv("COGNIS_TEST_POSTGRES_URL") else [])]
)
async def storage(request, tmp_path):
    postgres = request.param == "postgresql"
    engine = create_engine(
        os.environ["COGNIS_TEST_POSTGRES_URL"]
        if postgres
        else f"sqlite+aiosqlite:///{tmp_path / 'session-cleanup.db'}"
    )
    schema = "session_cleanup_" + uuid.uuid4().hex if postgres else None
    if schema:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = engine.execution_options(schema_translate_map={None: schema})
    metadata = MetaData()
    table = Table(
        "cleanup_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", Integer, nullable=False),
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(insert(table).values(id=1, value=0))
        yield engine, table
    finally:
        async with engine.begin() as connection:
            if schema:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            else:
                await connection.run_sync(metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [0, 1, 2])
@pytest.mark.parametrize("body_error", [False, True])
async def test_session_exit_owns_rollback_through_cancellation(storage, cancellations, body_error):
    engine, table = storage
    factory = create_session_factory(engine)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class GatedCloseSession(factory.class_):
        async def close(self) -> None:
            entered.set()
            await release.wait()
            await super().close()
            closed.set()

    gated_factory = async_sessionmaker(engine, class_=GatedCloseSession, expire_on_commit=False)

    async def write_then_exit():
        async with gated_factory() as session:
            await session.execute(update(table).where(table.c.id == 1).values(value=1))
            if body_error:
                raise ValueError("fixture body failure")

    operation = asyncio.create_task(write_then_exit())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for count in range(1, cancellations + 1):
            operation.cancel(f"cancel-{count}")
            # Queue after cancel delivery, without a wall-clock pause.
            delivered = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(delivered.set_result, None)
            await delivered
            assert operation.cancelling() == count
            assert not operation.done(), "Caller escaped while its rollback was still blocked"
        release.set()
        if cancellations:
            with pytest.raises(asyncio.CancelledError, match="cancel-1"):
                await operation
        elif body_error:
            with pytest.raises(ValueError, match="fixture body failure"):
                await operation
        else:
            await operation
        assert closed.is_set()
        async with factory() as session:
            assert await session.scalar(select(table.c.value)) == 0
            await session.execute(update(table).where(table.c.id == 1).values(value=2))
            await session.commit()
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await asyncio.wait_for(closed.wait(), 2)


@pytest.mark.asyncio
async def test_session_exit_preserves_body_cancellation(storage):
    engine, table = storage
    factory = create_session_factory(engine)
    body, entered, release, closed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    observed = []

    class GatedCloseSession(factory.class_):
        async def close(self) -> None:
            entered.set()
            await release.wait()
            await super().close()
            closed.set()

    gated_factory = async_sessionmaker(engine, class_=GatedCloseSession, expire_on_commit=False)

    async def write_until_cancelled():
        async with gated_factory() as session:
            await session.execute(update(table).where(table.c.id == 1).values(value=1))
            try:
                body.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                observed.append(exc)
                raise

    operation = asyncio.create_task(write_until_cancelled())
    try:
        await asyncio.wait_for(body.wait(), 2)
        operation.cancel("body cancellation")
        await asyncio.wait_for(entered.wait(), 2)
        assert operation.cancelling() == 1
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="body cancellation") as error:
            await operation
        assert error.value is observed[0]
        assert closed.is_set()
        async with factory() as session:
            assert await session.scalar(select(table.c.value)) == 0
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        await asyncio.wait_for(closed.wait(), 2)


@pytest.mark.asyncio
async def test_session_exit_propagates_cleanup_failure(storage):
    engine, table = storage
    factory = create_session_factory(engine)

    class FailingCloseSession(factory.class_):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("fixture cleanup failure")

    failing_factory = async_sessionmaker(engine, class_=FailingCloseSession, expire_on_commit=False)
    with pytest.raises(RuntimeError, match="fixture cleanup failure"):
        async with failing_factory() as session:
            await session.execute(update(table).where(table.c.id == 1).values(value=1))
    async with factory() as session:
        assert await session.scalar(select(table.c.value)) == 0


@pytest.mark.asyncio
async def test_bounded_shutdown_retains_blocked_session_cleanup(storage, caplog):
    engine, table = storage
    factory = create_session_factory(engine)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    coordinator = ShutdownCoordinator()

    class GatedCloseSession(factory.class_):
        async def close(self) -> None:
            entered.set()
            await release.wait()
            await super().close()
            closed.set()

    gated_factory = async_sessionmaker(engine, class_=GatedCloseSession, expire_on_commit=False)

    async def write_then_exit():
        async with gated_factory() as session:
            await session.execute(update(table).where(table.c.id == 1).values(value=1))

    async def begin():
        return None

    async def turns(_drain_timeout, _cancel_timeout):
        await operation
        return {"timed_out": 0}

    async def tools(_timeout):
        pytest.fail("Tool drain must not start after its settlement budget expires")

    operation = asyncio.create_task(write_then_exit())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        coordinator.configure(
            ShutdownCallbacks(begin, turns, tools),
            drain_timeout_seconds=0.01,
            cancel_timeout_seconds=0,
        )
        await asyncio.wait_for(coordinator.drain(), 1)
        assert not operation.done()
        assert not closed.is_set()
        assert len(coordinator._retained_tasks) == 1
        assert "shutdown stage ignored cancellation" in caplog.text
        retained = tuple(coordinator._retained_tasks)
        release.set()
        await asyncio.gather(*retained, return_exceptions=True)
        assert coordinator._retained_tasks == set()
        assert operation.cancelled()
        assert closed.is_set()
        async with factory() as session:
            assert await session.scalar(select(table.c.value)) == 0
            await session.execute(update(table).where(table.c.id == 1).values(value=2))
            await session.commit()
    finally:
        release.set()
        await asyncio.gather(operation, *coordinator._retained_tasks, return_exceptions=True)
        await asyncio.wait_for(closed.wait(), 2)
