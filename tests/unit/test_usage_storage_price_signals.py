"""Installed PostgreSQL LISTEN/NOTIFY commits, rollback and pricing-cache catch-up."""

import asyncio
import os

import pytest
from sqlalchemy import select, update

from cognis.core.cluster_signals import ClusterSignalService
from cognis.core.events import EventBus
from cognis.core.usage.price_publisher import UsagePricePinCache, UsagePricePublisher
from cognis.store.models import LLMProvider
from cognis.store.usage.transactions import notify_usage
from tests.unit.test_usage_storage_price_publisher import configured_info, lookup, prepare
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


@pytest.mark.asyncio
async def test_real_listener_only_rewarms_after_committed_configuration(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    if h.engine.dialect.name != "postgresql":
        pytest.skip("Real PostgreSQL transactional LISTEN/NOTIFY qualification")
    config = await prepare(h)
    cache = UsagePricePinCache()
    pricing_signal, usage_signal = asyncio.Event(), asyncio.Event()
    pricing_wakes = 0

    async def resolve(*_args, **_kwargs):
        async with h.sessions() as session:
            row = await session.scalar(select(LLMProvider))
            return configured_info(float(row.config.get("test_price", 2)))

    publisher = UsagePricePublisher(
        h.sessions, maintenance=h.maintenance, cache=cache, resolve_model=resolve
    )

    def pricing_wake():
        nonlocal pricing_wakes
        pricing_wakes += 1
        publisher.wake()
        pricing_signal.set()

    listener = ClusterSignalService(
        database_url=os.environ["COGNIS_TEST_POSTGRES_URL"],
        controller_id="price_signal_receiver",
        session_factory=h.sessions,
        event_bus=EventBus(),
        scope_provider=lambda: [],
        enabled=True,
        usage_waker=usage_signal.set,
        usage_pricing_waker=pricing_wake,
    )
    try:
        await publisher.start()
        assert await publisher.drain()
        await listener.start()
        await asyncio.wait_for(pricing_signal.wait(), 3)  # Real successful LISTEN catch-up.
        assert await publisher.drain()
        assert lookup(cache, config, configured_info()) is not None
        pricing_signal.clear()
        baseline = pricing_wakes

        class RollBack(Exception):
            pass

        with pytest.raises(RollBack):
            async with h.sessions.begin() as session:
                await publisher.notify_configuration(session)
                raise RollBack
        # Same connection/order domain delivery barrier; no timing-based "no signal" claim.
        async with h.sessions.begin() as session:
            await notify_usage(session, "price_signal_sender", "price_signal_rollback_barrier")
        await asyncio.wait_for(usage_signal.wait(), 3)
        assert pricing_wakes == baseline and not pricing_signal.is_set()

        changed = {**config, "test_price": 3}
        async with h.sessions.begin() as session:
            await session.execute(update(LLMProvider).values(config=changed))
            await publisher.notify_configuration(session)
            assert not pricing_signal.is_set()
        await asyncio.wait_for(pricing_signal.wait(), 3)
        assert await publisher.drain()
        assert pricing_wakes == baseline + 1
        assert lookup(cache, changed, configured_info(3)) is not None
        assert lookup(cache, changed, configured_info()) is None
    finally:
        await listener.stop()
        await publisher.aclose()
