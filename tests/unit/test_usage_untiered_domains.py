"""Retained tariff-domain invariants; ledger fixtures, not provider qualification."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

import cognis.core.usage.maintenance as maintenance_module
from cognis.core.usage.maintenance import UsageConflict, UsageJobStatus, UsageMaintenanceScope
from cognis.models.usage import (
    UsageOperationScope,
    UsagePriceDefinition,
    UsageUntieredTariffDeclaration,
)
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsageDailyTotalRow,
    UsageMaintenanceJobRow,
    UsagePriceRevisionRow,
    UsagePricingStateRow,
    UsageValuationRow,
)
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


def _price(
    h: UsageStorageHarness, *, basis: str = "configured_estimate", untiered: bool = False
) -> UsagePriceDefinition:
    data = h.price(f"upr_{uuid.uuid4().hex}").model_dump()
    data["basis"] = basis
    if untiered:
        data.update(
            rule_version=2,
            untiered_tariff=UsageUntieredTariffDeclaration(
                scope=UsageOperationScope(resource="openai.embeddings.create"),
                billing_basis="aggregate_input_tokens",
            ),
            rates=(data["rates"][0] | {"service_tier": None},),
        )
    return UsagePriceDefinition.model_validate(data)


async def _persisted(h: UsageStorageHarness) -> dict[str, list[dict[str, Any]]]:
    """Snapshot all persisted publication and money results around a rejection."""
    result = {}
    async with h.sessions() as session:
        for model in (
            UsagePriceRevisionRow,
            UsagePricingStateRow,
            UsageMaintenanceJobRow,
            UsageValuationRow,
            UsageArchivedTotalRow,
            UsageDailyTotalRow,
        ):
            table = model.__table__
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            result[table.name] = [dict(row) for row in rows.mappings()]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_first", [False, True], ids=["live", "archived"])
@pytest.mark.parametrize(
    ("original_basis", "rejected_basis"),
    [
        ("api_equivalent", "configured_estimate"),
        ("configured_estimate", "api_equivalent"),
    ],
    ids=["api-to-estimate", "estimate-to-api"],
)
async def test_disjoint_intermediate_cannot_reset_retained_overlapping_basis(
    usage_storage: UsageStorageHarness,
    monkeypatch: pytest.MonkeyPatch,
    archive_first: bool,
    original_basis: str,
    rejected_basis: str,
) -> None:
    h = usage_storage
    original = _price(h, basis=original_basis)
    await h.maintenance.publish_price(original)
    assert h.writer.try_emit(h.event(pin=original.revision_id))
    await h.pump()
    async with h.sessions() as session:
        fact = (await session.scalars(select(UsageAttemptRow))).one()
        scope = UsageMaintenanceScope(owner_key=fact.owner_key, bucket_day=fact.bucket_day)
        receipt = (await session.scalars(select(UsageValuationRow))).one()
        assert receipt.historical_cost == receipt.current_cost == Decimal("0.0024")
        assert receipt.basis == original_basis

    # A legacy contract and B's explicitly declared untiered contract are
    # disjoint. This valid adoption cannot erase A's retained basis constraint.
    intermediate = _price(h, untiered=True)
    await h.maintenance.publish_price(intermediate)
    await h.pump()

    if archive_first:

        class ArchiveDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return h.clock.now + timedelta(days=40)

        monkeypatch.setattr(maintenance_module, "datetime", ArchiveDatetime)
        await h.maintenance.request_archive(scope)
        await h.pump()
        await h.maintenance.request_rebuild(scope)
        await h.pump()

    async with h.sessions() as session:
        receipt = (await session.scalars(select(UsageValuationRow))).one()
        assert receipt.historical_cost == Decimal("0.0024") and receipt.current_cost is None
        assert receipt.basis == original_basis
        assert receipt.source_kind == ("archive" if archive_first else "attempt")
    before = await _persisted(h)

    rejected = _price(h, basis=rejected_basis)
    with pytest.raises(UsageConflict, match="cost basis"):
        await h.maintenance.publish_price(rejected)
    assert await _persisted(h) == before

    different_currency = _price(h, untiered=True).model_copy(update={"currency": "EUR"})
    with pytest.raises(UsageConflict, match="currency"):
        await h.maintenance.publish_price(different_currency)
    assert await _persisted(h) == before

    # Distinct compatible legacy and untiered publications still work after
    # rejection, and exact replay creates no new generation or queued work.
    compatible = _price(h, basis=original_basis)
    await h.maintenance.publish_price(compatible)
    await h.pump()
    async with h.sessions() as session:
        receipt = (await session.scalars(select(UsageValuationRow))).one()
        assert receipt.historical_cost == receipt.current_cost == Decimal("0.0024")
        assert receipt.basis == original_basis
    repeated = _price(h, untiered=True)
    await h.maintenance.publish_price(repeated)
    await h.pump()
    async with h.sessions() as session:
        receipt = (await session.scalars(select(UsageValuationRow))).one()
        assert receipt.historical_cost == Decimal("0.0024") and receipt.current_cost is None
        assert receipt.basis == original_basis
    replay_before = await _persisted(h)
    await h.maintenance.publish_price(repeated)
    assert await _persisted(h) == replay_before


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_untiered", [False, True], ids=["new-domain", "existing-domain"])
async def test_incompatible_concurrent_publications_share_the_existing_state_fence(
    usage_storage: UsageStorageHarness, seed_untiered: bool
) -> None:
    h = usage_storage
    seed = _price(h, untiered=True)
    if seed_untiered:
        await h.maintenance.publish_price(seed)
    prices = (_price(h, basis="api_equivalent"), _price(h, basis="configured_estimate"))
    start = asyncio.Event()

    async def publish(price: UsagePriceDefinition) -> UsageJobStatus:
        await start.wait()
        return await h.maintenance.publish_price(price)

    tasks = [asyncio.create_task(publish(price)) for price in prices]
    start.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(outcome, UsageJobStatus) for outcome in outcomes) == 1, outcomes
    assert sum(isinstance(outcome, UsageConflict) for outcome in outcomes) == 1, outcomes
    winner = prices[
        next(index for index, outcome in enumerate(outcomes) if isinstance(outcome, UsageJobStatus))
    ]
    successful = next(outcome for outcome in outcomes if isinstance(outcome, UsageJobStatus))
    assert successful.kind == "reprice" and successful.status == "queued"
    async with h.sessions() as session:
        state = await session.get(UsagePricingStateRow, (winner.provider_id, winner.model))
        assert state is not None
        assert state.current_revision_id == winner.revision_id
        assert state.generation == 1 + int(seed_untiered)
        persisted = set(await session.scalars(select(UsagePriceRevisionRow.revision_id)))
        assert persisted == {winner.revision_id} | ({seed.revision_id} if seed_untiered else set())
        jobs = list(await session.scalars(select(UsageMaintenanceJobRow)))
        assert len(jobs) == 1 + int(seed_untiered)
        assert {job.target_pricing_generation for job in jobs} == set(
            range(1, state.generation + 1)
        )
        assert (
            next(job for job in jobs if job.job_id == successful.job_id).target_pricing_generation
            == state.generation
        )
