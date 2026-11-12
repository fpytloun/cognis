"""Transaction-lease handoff and paused-retention recovery, without polling."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, select

from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.projector import UsageProjector, maintenance_boundary
from cognis.store.usage.models import UsageAttemptRow, UsageDailyTotalRow, UsageValuationRow
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["commit", "cancel"])
async def test_waiting_controller_keeps_its_wakeup_when_owner_releases_transaction_lease(
    usage_storage: UsageStorageHarness,
    release: str,
) -> None:
    h = usage_storage
    if h.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL transaction advisory-lock handoff")
    assert h.writer.try_emit(h.event())
    await h.writer.drain()
    held, resume, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with h.sessions.begin() as session:
            await maintenance_boundary(session)
            held.set()
            await resume.wait()

    owner = asyncio.create_task(holder())
    await asyncio.wait_for(held.wait(), 1)

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    contender = UsageProjector(h.sessions, controller_owner_id="second_controller")
    event.listen(h.engine.sync_engine, "before_cursor_execute", observe)
    try:
        await contender.start()
        drained = asyncio.create_task(contender.drain(timeout_seconds=2))
        await asyncio.wait_for(attempted.wait(), 1)
        if release == "cancel":
            owner.cancel()
        else:
            resume.set()
        with suppress(asyncio.CancelledError):
            await owner
        assert await drained
        async with h.sessions() as session:
            total = (await session.scalars(select(UsageDailyTotalRow))).one()
            assert total.finalized_count == 1
    finally:
        resume.set()
        owner.cancel()
        with suppress(asyncio.CancelledError):
            await owner
        event.remove(h.engine.sync_engine, "before_cursor_execute", observe)
        await contender.aclose()


@pytest.mark.asyncio
async def test_paused_archive_can_be_repaired_by_rebuild_without_killing_projector(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    h.clock.now -= timedelta(days=40)
    assert h.writer.try_emit(h.event(proxy="retained_until_repaired"))
    await h.pump()
    h.clock.now += timedelta(days=40)
    async with h.sessions.begin() as session:
        row = (await session.scalars(select(UsageAttemptRow))).one()
        scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
        await session.execute(delete(UsageValuationRow))
    archive = await h.maintenance.request_archive(scope)
    await h.pump()
    assert (await h.maintenance.status(archive.job_id)).status == "paused"
    await h.projector.start()
    assert await h.projector.drain(timeout_seconds=1)
    await h.maintenance.request_rebuild(scope)
    assert await h.projector.drain(timeout_seconds=2)
    async with h.sessions() as session:
        total = (await session.scalars(select(UsageDailyTotalRow))).one()
        assert total.finalized_count == 1
