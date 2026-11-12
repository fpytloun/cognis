"""Bounded shutdown keeps ownership until cancellation rolls back canonical work."""

import asyncio
from contextlib import suppress
from functools import partial

import pytest

from cognis.core.usage.capture_models import UsageWriterLimits
from cognis.core.usage.runtime import UsageRuntime
from cognis.core.usage.service import UsageAccountingService
from cognis.store.usage.models import UsageCoverageEpochRow
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_close", [False, True])
async def test_shutdown_retains_and_rejoins_slow_rollback_without_final_epoch_overlap(
    usage_storage: UsageStorageHarness,
    monkeypatch: pytest.MonkeyPatch,
    runtime_close: bool,
) -> None:
    h = usage_storage
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    limits = UsageWriterLimits(shutdown_seconds=0.1)
    runtime = None
    if runtime_close:
        monkeypatch.setattr(
            "cognis.core.usage.runtime.UsageAccountingService",
            partial(UsageAccountingService, limits=limits),
        )
        runtime = UsageRuntime(h.sessions, controller_owner_id="shutdown_join_test")
        service = runtime.service
        close = runtime.aclose
    else:
        service = UsageAccountingService(
            h.sessions,
            controller_owner_id="shutdown_join_test",
            projection_waker=h.projector.wake,
            limits=limits,
            clock=h.clock,
        )
        close = service.aclose
    closed_attempts = 0
    original_persist = service._persist_epoch

    async def persist(*, closed=False):
        nonlocal closed_attempts
        if closed:
            closed_attempts += 1
            assert service._task is None or service._task.done()
        await original_persist(closed=closed)

    async def batch_with_slow_rollback(_batch, _batch_id):
        async with h.sessions.begin() as session:
            session.add(
                UsageCoverageEpochRow(
                    epoch_id="shutdown_transaction_marker",
                    controller_owner_id=service.controller_owner_id,
                    started_at=h.clock.now,
                    coverage_status="open",
                )
            )
            await session.flush()
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set()
                await release.wait()
                raise

    monkeypatch.setattr(service, "_persist_epoch", persist)
    monkeypatch.setattr(service, "_commit_batch", batch_with_slow_rollback)
    if runtime is not None:
        await runtime.start()
    else:
        await service.start()
    assert await service.drain()
    assert service.try_emit(h.event())
    await asyncio.wait_for(entered.wait(), 2)
    worker = service._task
    assert worker is not None
    try:
        await asyncio.wait_for(close(), 1)
        assert cancelling.is_set()
        assert service._task is worker and not worker.done()
        assert worker.cancelling() == 1
        assert service.diagnostics().writer_failed
        assert service.diagnostics().writer_running and service.diagnostics().shutdown_pending
        assert service.diagnostics().pending_items == 1
        assert closed_attempts == 0
        release.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(worker), 2)
        assert service.diagnostics().shutdown_pending
        assert not service.diagnostics().writer_running
        await asyncio.wait_for(close(), 1)
        assert service._task is None
        assert not service.diagnostics().shutdown_pending
        assert service.diagnostics().dropped_finalized == 1
        assert closed_attempts == 1
        async with h.sessions() as session:
            assert await session.get(UsageCoverageEpochRow, "shutdown_transaction_marker") is None
            epoch = await session.get(UsageCoverageEpochRow, service.coverage_epoch_id)
            assert epoch.coverage_status == "gapped"
        await close()
        assert closed_attempts == 1
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(worker), 2)
        await close()
