"""Real page deadlines roll back before bounded event-driven pending-work retries."""

import asyncio

import pytest
from sqlalchemy import event, func, select

from cognis.core.usage.projector import UsagePendingTransactionTimeout
from cognis.store.usage.models import (
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageProjectionOutboxRow,
    UsageValuationRow,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


async def idle(h: UsageStorageHarness) -> None:
    # Unlike drain(), this barrier must not manufacture a recovery wake.
    await asyncio.wait_for(h.projector._idle.wait(), 5)


async def assert_rolled_back(h: UsageStorageHarness) -> None:
    async with h.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UsageDailyTotalRow)) == 0
        assert await session.scalar(select(func.count()).select_from(UsageValuationRow)) == 0
        outbox = await session.scalar(select(UsageProjectionOutboxRow))
        assert outbox.cursor == 0 and outbox.claim_epoch == 0 and outbox.completed_at is None
        attempt = await session.scalar(select(UsageAttemptRow))
        assert attempt.projected_version == 0


@pytest.mark.asyncio
async def test_partial_page_rolls_back_and_retries_without_another_wake(
    usage_storage: UsageStorageHarness, monkeypatch
) -> None:
    h = usage_storage
    assert h.writer.try_emit(h.event())
    await h.writer.drain()
    original = h.projector.apply_changes
    calls = 0
    checkouts = 0

    def checkout(*_args):
        nonlocal checkouts
        checkouts += 1

    def checkin(*_args):
        nonlocal checkouts
        checkouts -= 1

    event.listen(h.engine.sync_engine, "checkout", checkout)
    event.listen(h.engine.sync_engine, "checkin", checkin)
    original_page = h.projector.process_page

    async def page():
        if calls == 1:
            assert checkouts == 0
            await assert_rolled_back(h)
        return await original_page()

    async def apply(session, *args):
        nonlocal calls
        calls += 1
        await original(session, *args)
        await session.flush()
        if calls == 1:
            outbox = await session.scalar(select(UsageProjectionOutboxRow))
            outbox.cursor = 1
            await session.flush()
            await asyncio.Event().wait()  # The installed, unchanged 0.5s budget cancels this.

    monkeypatch.setattr(h.projector, "apply_changes", apply)
    monkeypatch.setattr(h.projector, "process_page", page)
    try:
        await h.projector.start()
        await idle(h)
        health = h.projector.diagnostics
        assert not health.failed and health.paused_error_code is None
        assert health.timeout_retries == 1 and not health.retry_exhausted
        assert h.projector.page_rows == 2
        async with h.sessions() as session:
            total = await session.scalar(select(UsageDailyTotalRow))
            assert total.finalized_count == 1 and total.input_tokens == 1000
            assert await session.scalar(select(func.count()).select_from(UsageValuationRow)) == 1
            outbox = await session.scalar(select(UsageProjectionOutboxRow))
            assert outbox.cursor == 1 and outbox.claim_epoch == 1
            assert outbox.completed_at is not None
        assert calls == 2
    finally:
        event.remove(h.engine.sync_engine, "checkout", checkout)
        event.remove(h.engine.sync_engine, "checkin", checkin)


@pytest.mark.asyncio
async def test_exhaustion_stops_with_inspectable_health(
    usage_storage: UsageStorageHarness, monkeypatch
) -> None:
    h = usage_storage
    assert h.writer.try_emit(h.event())
    await h.writer.drain()
    original = h.projector.apply_changes
    calls = 0

    async def apply(session, *args):
        nonlocal calls
        calls += 1
        await original(session, *args)
        await session.flush()
        await asyncio.Event().wait()

    monkeypatch.setattr(h.projector, "apply_changes", apply)
    await h.projector.start()
    await idle(h)
    health = h.projector.diagnostics
    assert health.running and not health.failed
    assert health.paused_error_code == UsagePendingTransactionTimeout.__name__
    assert health.timeout_retries == 2 and health.retry_exhausted and not health.retrying
    assert calls == 3 and h.projector.page_rows == 1
    await assert_rolled_back(h)
    task = h.projector._task
    assert task is not None and not task.done()
    await asyncio.wait_for(h.projector.aclose(), 1)


@pytest.mark.asyncio
async def test_permanent_validation_error_is_not_retried(
    usage_storage: UsageStorageHarness, monkeypatch
) -> None:
    h = usage_storage
    calls = 0

    async def broken():
        nonlocal calls
        calls += 1
        raise ValueError("not_a_transient_deadline")

    monkeypatch.setattr(h.projector, "process_page", broken)
    await h.projector.start()
    await idle(h)
    assert calls == 1
    assert h.projector.diagnostics.paused_error_code == "ValueError"
    assert h.projector.diagnostics.timeout_retries == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_backoff_without_a_session_or_retry(
    usage_storage: UsageStorageHarness, monkeypatch
) -> None:
    h = usage_storage
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == 0.05:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            await original_sleep(delay)

    async def timeout():
        raise UsagePendingTransactionTimeout

    monkeypatch.setattr(h.projector, "process_page", timeout)
    monkeypatch.setattr("cognis.core.usage.projector.asyncio.sleep", sleep)
    await h.projector.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(h.projector.aclose(), 1)
    assert cancelled.is_set()
    assert h.projector.diagnostics.timeout_retries == 1
    assert not h.projector.diagnostics.retrying
