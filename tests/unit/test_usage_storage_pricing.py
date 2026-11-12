"""Immutable snapshots, explicit partial values and archive repricing invariants."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cognis.core.usage.maintenance import UsageConflict, UsageMaintenanceScope
from cognis.core.usage.pricing import calculate_price
from cognis.models.usage import NormalizedUsage, UsagePriceRate
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageValuationRow,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_reactivating_immutable_price_reprices_again(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    first = h.price()
    await h.maintenance.publish_price(first)
    assert h.writer.try_emit(h.event(pin=first.revision_id))
    await h.pump()
    await h.maintenance.publish_price(h.price("price_2", multiplier=2))
    await h.pump()
    await h.maintenance.publish_price(first)
    await h.pump()
    async with h.sessions() as session:
        row = (await session.scalars(select(UsageValuationRow))).one()
        assert row.current_cost == row.historical_cost == Decimal("0.0024")
        assert row.pricing_generation == 3
    with pytest.raises(UsageConflict):
        await h.maintenance.publish_price(
            first.model_copy(
                update={
                    "revision_id": "price_eur",
                    "currency": "EUR",
                }
            )
        )


@pytest.mark.asyncio
async def test_archive_verifies_old_receipt_while_new_price_waits(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    h.clock.now -= timedelta(days=40)
    await h.maintenance.publish_price(h.price())
    assert h.writer.try_emit(h.event(pin="price_1", proxy="old_price_session"))
    await h.pump()
    async with h.sessions() as session:
        row = (await session.scalars(select(UsageAttemptRow))).one()
        scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
    archive = await h.maintenance.request_archive(scope)
    await h.maintenance.publish_price(h.price("price_2", multiplier=2))
    await h.pump()
    assert (await h.maintenance.status(archive.job_id)).status == "complete"
    async with h.sessions() as session:
        assert await session.scalar(select(UsageAttemptRow)) is None
        archived = (await session.scalars(select(UsageArchivedTotalRow))).one()
        receipt = (
            await session.scalars(
                select(UsageValuationRow).where(UsageValuationRow.source_kind == "archive")
            )
        ).one()
        assert archived.historical_cost == receipt.historical_cost == Decimal("0.0024")
        assert receipt.current_cost == Decimal("0.0048")


@pytest.mark.asyncio
async def test_price_tiers_use_inclusive_input_and_do_not_guess_unknown_prompt(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    event = h.event()
    price = h.price().model_copy(
        update={
            "rates": (
                UsagePriceRate(dimension="input", per_million=2, service_tier="standard"),
                UsagePriceRate(
                    dimension="input",
                    per_million=4,
                    input_threshold=1200,
                    service_tier="standard",
                ),
                UsagePriceRate(dimension="cache_read", per_million=1, service_tier="standard"),
                UsagePriceRate(dimension="output", per_million=4, service_tier="standard"),
            )
        }
    )
    data = event.usage.model_dump()
    data.update(
        cache_read_semantics="additional_to_input",
        cache_read_tokens={"value": 500, "status": "reported", "source": "provider"},
    )
    result = calculate_price(NormalizedUsage.model_validate(data), event.context.selection, price)
    assert result.cost == Decimal("0.0049")
    data["input_tokens"] = {"status": "unknown"}
    unknown = calculate_price(NormalizedUsage.model_validate(data), event.context.selection, price)
    assert unknown.status != "complete"
    assert dict(unknown.units).get("input") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [None, "auto"])
@pytest.mark.parametrize("zero_units", [False, True])
async def test_archive_repricing_preserves_unknown_tier_coverage(
    usage_storage: UsageStorageHarness,
    tier: str | None,
    zero_units: bool,
) -> None:
    h = usage_storage
    h.clock.now -= timedelta(days=40)
    await h.maintenance.publish_price(h.price())
    event = h.event(pin="price_1")
    selection = event.context.selection.model_copy(
        update={
            "service_tier": tier,
            "effective_parameters": event.context.selection.effective_parameters.model_copy(
                update={"service_tier": tier}
            ),
        }
    )
    usage = event.usage
    if zero_units:
        usage = usage.model_copy(
            update={
                "input_tokens": usage.input_tokens.model_copy(update={"value": 0}),
                "output_tokens": usage.output_tokens.model_copy(update={"value": 0}),
            }
        )
    event = event.model_copy(
        update={
            "usage": usage,
            "context": event.context.model_copy(update={"selection": selection}),
        }
    )
    assert h.writer.try_emit(event)
    await h.pump()
    async with h.sessions() as session:
        row = (await session.scalars(select(UsageAttemptRow))).one()
        scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
    archive = await h.maintenance.request_archive(scope)
    await h.pump()
    assert (await h.maintenance.status(archive.job_id)).status == "complete"
    await h.maintenance.publish_price(h.price("price_2", multiplier=2))
    await h.pump()
    async with h.sessions() as session:
        receipt = (
            await session.scalars(
                select(UsageValuationRow).where(UsageValuationRow.source_kind == "archive")
            )
        ).one()
        assert receipt.historical_cost is None and receipt.historical_status == "unpriced"
        assert receipt.current_cost is None and receipt.current_status == "unpriced"
        daily = (await session.scalars(select(UsageDailyTotalRow))).one()
        assert daily.finalized_count == 1
        assert daily.historical_priced_count == daily.current_priced_count == 0
        assert daily.historical_partial_count == daily.current_partial_count == 0
