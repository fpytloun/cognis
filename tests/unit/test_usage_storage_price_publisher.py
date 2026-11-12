"""Committed pricing pins, normalized source coverage, races and bounded worker lifetime."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from cognis.core.usage.price_publisher import (
    MAX_PRICE_PINS,
    UsagePricePinCache,
    UsagePricePublisher,
    metadata_price,
)
from cognis.core.usage.pricing import calculate_price
from cognis.models.config import ModelInfo
from cognis.models.usage_pricing import UsagePricingPin, usage_pricing_fingerprint
from cognis.providers.llm.model_metadata import resolve_model_metadata
from cognis.store.models import LLMProvider, User
from cognis.store.usage.models import UsagePriceRevisionRow, UsageValuationRow
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


async def prepare(h: UsageStorageHarness) -> dict:
    async with h.engine.begin() as connection:
        await connection.run_sync(User.__table__.create, checkfirst=True)
        await connection.run_sync(LLMProvider.__table__.create, checkfirst=True)
    config = {
        "preset": "openai",
        "default_model": "gpt-4.1",
        "models": [{"model_id": "gpt-4.1", "tier": "standard"}],
    }
    async with h.sessions.begin() as session:
        session.add(User(email="pricing@cognis.test", is_active=False))
        await session.flush()
        session.add(
            LLMProvider(
                provider_id="pricing",
                display_name="Pricing",
                owner_email="pricing@cognis.test",
                location="controller",
                backend="litellm",
                config=config,
                status="active",
            )
        )
    return config


def configured_info(price: float = 2) -> ModelInfo:
    return ModelInfo(
        model_id="gpt-4.1",
        input_cost_per_mtok=price,
        output_cost_per_mtok=4,
        provider_metadata={
            "field_sources": {
                "input_cost_per_mtok": "configured",
                "output_cost_per_mtok": "configured",
                "tier": "configured",
            }
        },
    )


def lookup(cache: UsagePricePinCache, config: dict, info: ModelInfo) -> UsagePricingPin | None:
    return cache(
        provider_id="pricing",
        model="gpt-4.1",
        metadata_fingerprint=usage_pricing_fingerprint(
            provider_id="pricing",
            model="gpt-4.1",
            model_info=info,
            provider_config=config,
            service_tier=info.tier,
        ),
    )


@pytest.mark.asyncio
async def test_normalized_catalog_warms_and_concurrent_restart_reuses_committed_revision(
    usage_storage: UsageStorageHarness, monkeypatch
) -> None:
    h = usage_storage
    config = await prepare(h)
    monkeypatch.setattr(
        "cognis.providers.llm.model_metadata.litellm.get_model_info",
        lambda **_: {
            "litellm_provider": "openai",
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.000004,
        },
    )
    info = ModelInfo(
        model_id="gpt-4.1",
        **resolve_model_metadata("gpt-4.1", preset="openai", remote={"tier": "standard"}),
    )
    caches = [UsagePricePinCache(), UsagePricePinCache()]
    publishers = [
        UsagePricePublisher(
            h.sessions,
            maintenance=h.maintenance,
            cache=cache,
            resolve_model=AsyncMock(return_value=info),
        )
        for cache in caches
    ]
    try:
        await asyncio.gather(*(publisher.start() for publisher in publishers))
        assert all(await asyncio.gather(*(publisher.drain() for publisher in publishers)))
        pin = lookup(caches[0], config, info)
        assert pin is not None and pin == lookup(caches[1], config, info)
        async with h.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(UsagePriceRevisionRow)) == 1
            )
            row = await session.get(UsagePriceRevisionRow, pin.captured_price_revision_id)
            assert row.source_revision == pin.resolver_snapshot_id
            assert row.provenance == "catalog"
            assert {item["dimension"] for item in row.definition["rates"]} == {"input", "output"}
        assert lookup(caches[0], config, configured_info(99)) is None
    finally:
        await asyncio.gather(*(publisher.aclose() for publisher in publishers))


@pytest.mark.asyncio
async def test_config_change_during_resolution_pauses_unpriced_then_new_wake_recovers(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    config = await prepare(h)
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return configured_info()

    cache = UsagePricePinCache()
    publisher = UsagePricePublisher(
        h.sessions, maintenance=h.maintenance, cache=cache, resolve_model=resolve
    )
    try:
        await publisher.start()
        await asyncio.wait_for(entered.wait(), 2)
        assert lookup(cache, config, configured_info()) is None
        changed = {**config, "models": [{"model_id": "gpt-4.1", "input_cost_per_mtok": 3}]}
        async with h.sessions.begin() as session:
            await session.execute(update(LLMProvider).values(config=changed))
        release.set()
        assert not await publisher.drain()
        assert publisher.diagnostics.paused_error_code == "UsagePriceSourceChanged"
        async with h.sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(UsagePriceRevisionRow)) == 0
            )
        publisher.resolve_model = AsyncMock(return_value=configured_info(3))
        publisher.wake()
        assert lookup(cache, config, configured_info()) is None
        assert await publisher.drain()
        assert lookup(cache, changed, configured_info(3)) is not None
        assert lookup(cache, changed, configured_info()) is None
        assert lookup(cache, config, configured_info()) is None
    finally:
        await publisher.aclose()


@pytest.mark.asyncio
async def test_publication_failure_never_exposes_prepared_pin_or_logs_payload(
    usage_storage: UsageStorageHarness, monkeypatch, caplog
) -> None:
    h = usage_storage
    config = await prepare(h)
    cache = UsagePricePinCache()

    async def fail(*_args, **_kwargs):
        raise RuntimeError("private_provider_configuration")

    monkeypatch.setattr(h.maintenance, "publish_price", fail)
    publisher = UsagePricePublisher(
        h.sessions,
        maintenance=h.maintenance,
        cache=cache,
        resolve_model=AsyncMock(return_value=configured_info()),
    )
    try:
        await publisher.start()
        assert not await publisher.drain()
        assert lookup(cache, config, configured_info()) is None
        assert publisher.diagnostics.paused_error_code == "RuntimeError"
        assert "private_provider_configuration" not in caplog.text
    finally:
        await publisher.aclose()


@pytest.mark.asyncio
async def test_close_cancels_metadata_wait_and_clears_cache(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    await prepare(h)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def resolve(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cache = UsagePricePinCache()
    publisher = UsagePricePublisher(
        h.sessions, maintenance=h.maintenance, cache=cache, resolve_model=resolve
    )
    await publisher.start()
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(publisher.aclose(), 1)
    assert cancelled.is_set() and len(cache) == 0
    assert not publisher.diagnostics.running


def test_cache_snapshot_is_bounded_and_detached_from_input() -> None:
    cache = UsagePricePinCache()
    pin = UsagePricingPin(
        captured_price_revision_id="price_revision", resolver_snapshot_id="snapshot"
    )
    pins = {("provider", str(index), "fingerprint"): pin for index in range(MAX_PRICE_PINS)}
    cache.replace(pins)
    pins.clear()
    assert len(cache) == MAX_PRICE_PINS
    with pytest.raises(ValueError):
        cache.replace(
            {("provider", str(index), "fingerprint"): pin for index in range(MAX_PRICE_PINS + 1)}
        )
    cache.invalidate()
    assert len(cache) == 0


def test_context_and_cache_rates_preserve_rules_and_unknown_tiers() -> None:
    info = configured_info()
    info.pricing = {
        "input_cost_per_token_above_200k_tokens": 0.000004,
        "cache_creation_input_token_cost": 0.000003,
        "cache_creation_input_token_cost_1hr": 0.000006,
    }
    info.provider_metadata["field_sources"]["pricing"] = "provider"
    _, definition = metadata_price(
        provider_id="pricing",
        model=info.model_id,
        info=info,
        config={},
        now=datetime.now(UTC),
    )
    assert definition is not None
    assert any(
        rate.dimension == "input" and rate.input_threshold == 200001 for rate in definition.rates
    )
    assert any(
        rate.dimension == "cache_write_1h" and rate.per_million == 6 for rate in definition.rates
    )
    info.pricing["tiered_pricing"] = [{"unrecognized_rule": 4}]
    _, unsupported = metadata_price(
        provider_id="pricing", model=info.model_id, info=info, config={}, now=definition.recorded_at
    )
    assert unsupported is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier", [None, "auto", "unknown", "default", "standard", "priority", "flex"]
)
async def test_current_valuation_cannot_apply_standard_metadata_to_another_tier(
    usage_storage: UsageStorageHarness, tier: str | None
) -> None:
    h = usage_storage
    _, definition = metadata_price(
        provider_id="pricing",
        model="gpt-4.1",
        info=configured_info(),
        config={},
        now=h.clock.now - timedelta(seconds=1),
    )
    assert definition is not None
    assert all(rate.service_tier == "standard" for rate in definition.rates)
    await h.maintenance.publish_price(definition)
    event = h.event()
    event = event.model_copy(
        update={
            "context": event.context.model_copy(
                update={
                    "selection": event.context.selection.model_copy(
                        update={
                            "provider_id": "pricing",
                            "effective_model": "gpt-4.1",
                            "service_tier": tier,
                        }
                    )
                }
            )
        }
    )
    assert h.writer.try_emit(event)
    await h.pump()
    async with h.sessions() as session:
        receipt = (await session.scalars(select(UsageValuationRow))).one()
        assert receipt.historical_cost is None
        if tier not in {"default", "standard"}:
            assert receipt.current_cost is None and receipt.current_status == "unpriced"
        else:
            assert (
                receipt.current_cost == Decimal("0.0024") and receipt.current_status == "complete"
            )


@pytest.mark.parametrize("tier_source", [None, "default", "model_policy"])
def test_default_metadata_tier_prices_requests_that_name_no_tier(
    tier_source: str | None,
) -> None:
    info = configured_info()
    if tier_source is None:
        info.provider_metadata["field_sources"].pop("tier")
    else:
        info.provider_metadata["field_sources"]["tier"] = tier_source
    assert info.tier == "standard"
    _, definition = metadata_price(
        provider_id="pricing", model=info.model_id, info=info, config={}, now=datetime.now(UTC)
    )
    assert definition is not None
    assert definition.revision_id.startswith("upr_v4_")
    assert {rate.service_tier for rate in definition.rates} == {"standard"}


def test_other_tier_and_free_price_keys_do_not_block_default_tier_rates() -> None:
    info = configured_info()
    info.pricing = {
        "input_cost_per_token_batches": 1e-06,
        "output_cost_per_token_flex": 2e-06,
        "input_cost_per_token_priority": 3e-06,
        "image": "0",
        "request": 0,
    }
    info.provider_metadata["field_sources"]["pricing"] = "configured"
    _, definition = metadata_price(
        provider_id="pricing", model=info.model_id, info=info, config={}, now=datetime.now(UTC)
    )
    assert definition is not None
    assert all(rate.service_tier == "standard" for rate in definition.rates)
    info.pricing = {
        "search_context_cost_per_query": {"search_context_size_low": 0.01},
        "cache_creation_input_token_cost_above_12hr": 5e-06,
        "cache_creation_input_token_cost_above_1hr": 8e-06,
    }
    _, token_model = metadata_price(
        provider_id="pricing", model=info.model_id, info=info, config={}, now=datetime.now(UTC)
    )
    assert token_model is not None
    assert any(
        rate.dimension == "cache_write" and rate.cache_ttl == "1h" for rate in token_model.rates
    )
    unmetered = configured_info(price=0)
    unmetered.output_cost_per_mtok = 0
    unmetered.pricing = {"input_cost_per_second": "0.0001"}
    unmetered.provider_metadata["field_sources"]["pricing"] = "configured"
    _, per_second = metadata_price(
        provider_id="pricing",
        model=unmetered.model_id,
        info=unmetered,
        config={},
        now=datetime.now(UTC),
    )
    assert per_second is None


@pytest.mark.parametrize("wire_tier", [None, "auto", "unknown", "priority"])
@pytest.mark.parametrize("zero_units", [False, True])
def test_null_tariff_is_never_an_applicability_wildcard(
    usage_storage: UsageStorageHarness, wire_tier: str | None, zero_units: bool
) -> None:
    from cognis.core.usage.pricing import calculate_price

    event = usage_storage.event()
    selection = event.context.selection.model_copy(update={"service_tier": wire_tier})
    definition = usage_storage.price()
    definition = definition.model_copy(
        update={
            "rates": tuple(
                rate.model_copy(update={"service_tier": None}) for rate in definition.rates
            )
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
    result = calculate_price(usage, selection, definition)
    assert result.cost is None and result.status == "unpriced"


@pytest.mark.parametrize("suffix", ["200k", "200000"])
@pytest.mark.parametrize("input_tokens", [200000, 200001])
@pytest.mark.asyncio
async def test_native_above_threshold_remains_strict_for_input_and_cache_aliases(
    usage_storage: UsageStorageHarness, suffix: str, input_tokens: int
) -> None:
    h = usage_storage
    info = configured_info()
    info.cache_write_cost_per_mtok = 3
    info.provider_metadata["field_sources"].update(
        cache_write_cost_per_mtok="provider", pricing="provider"
    )
    info.pricing = {
        f"input_cost_per_token_above_{suffix}_tokens": 0.000004,
        "cache_creation_input_token_cost": 0.000003,
        f"cache_creation_input_token_cost_above_{suffix}_tokens": 0.000006,
    }
    _, definition = metadata_price(
        provider_id="pricing", model="gpt-4.1", info=info, config={}, now=h.clock.now
    )
    assert definition is not None
    from cognis.core.usage.pricing import price_rate

    for dimension in ("input", "cache_write", "cache_write_5m"):
        rate = price_rate(
            definition,
            dimension,
            service_tier="default",
            cache_ttl="5m",
            input_tokens=input_tokens,
        )
        assert rate is not None
        expected = (
            (2 if dimension == "input" else 3)
            if input_tokens == 200000
            else (4 if dimension == "input" else 6)
        )
        assert rate.per_million == expected
    usage = h.event().usage.model_copy(
        update={
            "input_tokens": h.event().usage.input_tokens.model_copy(update={"value": input_tokens})
        }
    )
    selection = h.event().context.selection.model_copy(
        update={
            "provider_id": "pricing",
            "effective_model": "gpt-4.1",
        }
    )
    valuation = calculate_price(usage, selection, definition)
    assert valuation.cost == (
        Decimal(input_tokens) * (2 if input_tokens == 200000 else 4) / 1_000_000 + Decimal("0.0004")
    )


@pytest.mark.asyncio
async def test_corrected_compiler_preserves_old_immutable_revision(
    usage_storage: UsageStorageHarness,
) -> None:
    h = usage_storage
    config = await prepare(h)
    fingerprint, definition = metadata_price(
        provider_id="pricing",
        model="gpt-4.1",
        info=configured_info(),
        config=config,
        now=h.clock.now,
    )
    assert definition is not None
    legacy = definition.model_copy(
        update={
            "revision_id": "upr_" + fingerprint[4:],
            "rates": tuple(
                rate.model_copy(update={"service_tier": None}) for rate in definition.rates
            ),
        }
    )
    await h.maintenance.publish_price(legacy)
    cache = UsagePricePinCache()
    publisher = UsagePricePublisher(
        h.sessions,
        maintenance=h.maintenance,
        cache=cache,
        resolve_model=AsyncMock(return_value=configured_info()),
    )
    try:
        await publisher.start()
        assert await publisher.drain()
        pin = lookup(cache, config, configured_info())
        assert pin is not None and pin.captured_price_revision_id == definition.revision_id
        assert pin.captured_price_revision_id != legacy.revision_id
        async with h.sessions() as session:
            old = await session.get(UsagePriceRevisionRow, legacy.revision_id)
            assert old.definition == legacy.model_dump(mode="json", exclude_none=True)
            assert (
                await session.scalar(select(func.count()).select_from(UsagePriceRevisionRow)) == 2
            )
    finally:
        await publisher.aclose()


@pytest.mark.asyncio
async def test_resolution_timeout_does_not_evict_unrelated_committed_prices(
    usage_storage: UsageStorageHarness,
    caplog,
) -> None:
    h = usage_storage
    config = await prepare(h)
    changed = {**config, "models": [{"model_id": "a-slow"}, {"model_id": "gpt-4.1"}]}
    async with h.sessions.begin() as session:
        await session.execute(update(LLMProvider).values(config=changed))

    async def resolve(model, **_kwargs):
        if model == "a-slow":
            raise TimeoutError("private_upstream_payload")
        return configured_info()

    cache = UsagePricePinCache()
    publisher = UsagePricePublisher(
        h.sessions, maintenance=h.maintenance, cache=cache, resolve_model=resolve
    )
    try:
        await publisher.start()
        assert await publisher.drain()
        original = lookup(cache, changed, configured_info())
        assert original is not None
        publisher.wake()
        assert await publisher.drain()
        assert lookup(cache, changed, configured_info()) == original
        assert publisher.diagnostics.resolution_failures == 1
        assert publisher.diagnostics.resolution_error_code == "TimeoutError"
        assert publisher.diagnostics.paused_error_code is None
        assert lookup(cache, changed, configured_info(99)) is None
        assert "private_upstream_payload" not in caplog.text
    finally:
        await publisher.aclose()


@pytest.mark.asyncio
async def test_failed_refresh_keeps_committed_prices_and_retries_without_a_new_wake(
    usage_storage: UsageStorageHarness, monkeypatch
) -> None:
    h = usage_storage
    config = await prepare(h)
    monkeypatch.setattr("cognis.core.usage.price_publisher.PUBLICATION_RETRY_SECONDS", 0.05)
    cache = UsagePricePinCache()
    publisher = UsagePricePublisher(
        h.sessions,
        maintenance=h.maintenance,
        cache=cache,
        resolve_model=AsyncMock(return_value=configured_info()),
    )
    original_publish = h.maintenance.publish_price
    failures = 0

    async def flaky(*args, **kwargs):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise TimeoutError
        return await original_publish(*args, **kwargs)

    try:
        await publisher.start()
        assert await publisher.drain()
        committed = lookup(cache, config, configured_info())
        assert committed is not None
        monkeypatch.setattr(h.maintenance, "publish_price", flaky)
        # A configuration save or credential refresh must not unprice requests meanwhile.
        publisher.wake()
        assert lookup(cache, config, configured_info()) == committed
        assert not await publisher.drain(timeout_seconds=1)
        assert publisher.diagnostics.paused_error_code == "TimeoutError"
        assert lookup(cache, config, configured_info()) == committed
        # The failed publication retries on its own.
        for _ in range(100):
            if publisher.diagnostics.paused_error_code is None and await publisher.drain(
                timeout_seconds=0.05
            ):
                break
            await asyncio.sleep(0.02)
        assert publisher.diagnostics.paused_error_code is None
        assert lookup(cache, config, configured_info()) == committed
    finally:
        await publisher.aclose()
