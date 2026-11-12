"""Canonical facts/prices/archive survive loss of every disposable projection."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from cognis.core.usage.maintenance import UsageConflict, UsageMaintenanceScope
from cognis.models.usage import UsageAttemptStarted, UsageCorrection
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageHistoricalPriceBindingRow,
    UsageProjectionStateRow,
    UsageSessionTotalRow,
    UsageValuationRow,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_storage_price_revision_late_finalize_and_backfill_rebuild(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    await h.maintenance.publish_price(h.price())
    event = h.event(pin="price_1")
    assert h.writer.try_emit(UsageAttemptStarted(observation_id="start_1", context=event.context))
    await h.pump()
    await h.maintenance.publish_price(h.price("price_2", multiplier=2))
    await h.pump()
    assert h.writer.try_emit(event)
    await h.pump()
    async with h.sessions() as session:
        valuation = await session.scalar(
            select(UsageValuationRow).where(UsageValuationRow.source_kind == "attempt")
        )
        assert valuation.historical_cost == Decimal("0.0024")
        assert valuation.current_cost == Decimal("0.0048")
    missing = h.event(2)
    assert h.writer.try_emit(missing)
    await h.pump()
    await h.maintenance.request_backfill(
        UsageMaintenanceScope(
            provider_id=event.context.selection.provider_id,
            model=event.context.selection.effective_model,
        )
    )
    await h.pump()
    async with h.sessions() as session:
        assert (
            await session.scalar(
                select(UsageHistoricalPriceBindingRow).where(
                    UsageHistoricalPriceBindingRow.attempt_id == missing.context.trace.attempt_id
                )
            )
            is not None
        )
        row = await session.scalar(select(UsageAttemptRow))
        owner, day = row.owner_key, row.bucket_day
    async with h.sessions.begin() as session:
        await session.execute(delete(UsageValuationRow))
    await h.maintenance.request_rebuild(UsageMaintenanceScope(owner_key=owner, bucket_day=day))
    await h.pump()
    async with h.sessions() as session:
        values = (
            await session.scalars(
                select(UsageValuationRow).where(
                    UsageValuationRow.source_kind == "attempt",
                )
            )
        ).all()
        assert any(
            value.source_id == missing.context.trace.attempt_id
            and value.historical_revision_id is not None
            for value in values
        )


@pytest.mark.asyncio
async def test_storage_proxy_retirement_keeps_late_finalized_same_start_day(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    h.clock.now -= timedelta(days=40)
    old = h.event(1, proxy="unverified_session")
    late = h.event(2, proxy="unverified_session")
    assert h.writer.try_emit(old)
    assert h.writer.try_emit(UsageAttemptStarted(observation_id="pending_2", context=late.context))
    await h.pump()
    h.clock.now += timedelta(days=40)
    assert h.writer.try_emit(late)
    await h.pump()
    async with h.sessions() as session:
        row = await session.scalar(select(UsageAttemptRow))
        scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
        total = await session.scalar(
            select(UsageSessionTotalRow).where(UsageSessionTotalRow.session_kind == "proxy")
        )
        assert total.finalized_count == 2
    await h.maintenance.request_archive(scope)
    await h.pump()
    async with h.sessions() as session:
        assert (
            await session.scalar(
                select(UsageAttemptRow).where(
                    UsageAttemptRow.attempt_id == old.context.trace.attempt_id
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(UsageAttemptRow).where(
                    UsageAttemptRow.attempt_id == late.context.trace.attempt_id
                )
            )
            is not None
        )
        total = await session.scalar(
            select(UsageSessionTotalRow).where(UsageSessionTotalRow.session_kind == "proxy")
        )
        assert total.finalized_count == 1
        archive = await session.scalar(select(UsageArchivedTotalRow))
        assert "unverified_session" not in str(archive.dimensions)
        daily = await session.scalar(select(UsageDailyTotalRow))
        assert daily.finalized_count == 2
    # Expired replay must never resurrect compacted facts.
    assert h.writer.try_emit(old)
    await h.pump()
    async with h.sessions() as session:
        assert (
            await session.scalar(
                select(UsageAttemptRow).where(
                    UsageAttemptRow.attempt_id == old.context.trace.attempt_id
                )
            )
            is None
        )
    await h.maintenance.request_rebuild(scope)
    await h.pump()
    async with h.sessions() as session:
        proxy = (
            await session.scalars(
                select(UsageSessionTotalRow).where(
                    UsageSessionTotalRow.session_kind == "proxy",
                    UsageSessionTotalRow.finalized_count > 0,
                )
            )
        ).all()
        assert sum(value.finalized_count for value in proxy) == 1


@pytest.mark.asyncio
async def test_rebuild_includes_unprojected_and_catches_late_pending_final(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    pending = h.event(1)
    assert h.writer.try_emit(
        UsageAttemptStarted(observation_id="pending_first", context=pending.context)
    )
    assert h.writer.try_emit(h.event(2))
    await h.writer.drain()
    async with h.sessions() as session:
        row = (await session.scalars(select(UsageAttemptRow))).first()
        scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
    job = await h.maintenance.request_rebuild(scope)
    assert await h.maintenance.process_page()
    assert await h.maintenance.process_page()
    assert h.writer.try_emit(pending)
    await h.writer.drain()
    await h.pump()
    assert (await h.maintenance.status(job.job_id)).status == "complete"
    async with h.sessions() as session:
        state = (await session.scalars(select(UsageProjectionStateRow))).one()
        assert state.active_generation == 2 and state.ready
        totals = (
            await session.scalars(
                select(UsageDailyTotalRow).where(UsageDailyTotalRow.generation == 2)
            )
        ).all()
        assert sum(row.finalized_count for row in totals) == 2


@pytest.mark.asyncio
async def test_correction_is_cas_and_idempotent(usage_storage: UsageStorageHarness) -> None:
    h = usage_storage
    original = h.event()
    assert h.writer.try_emit(original)
    await h.pump()
    data = original.model_dump()
    data["usage"]["input_tokens"]["value"] = 2000
    command = UsageCorrection(
        correction_id="correct_input_1",
        expected_fact_version=1,
        reason="late_provider_usage",
        replacement=type(original).model_validate(data),
    )
    assert await h.maintenance.correct(command) == 2
    assert await h.maintenance.correct(command) == 2
    await h.pump()
    async with h.sessions() as session:
        total = (await session.scalars(select(UsageDailyTotalRow))).one()
        assert total.finalized_count == 1 and total.input_tokens == 2000
    with pytest.raises(UsageConflict):
        await h.maintenance.correct(command.model_copy(update={"correction_id": "stale_1"}))
