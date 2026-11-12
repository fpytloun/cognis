"""Pure decimal valuation with explicit semantic and missing-price coverage."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cognis.models.usage import (
    NormalizedUsage,
    UsageModelSelection,
    UsageNumericExtra,
    UsagePriceDefinition,
    UsagePriceRate,
)
from cognis.models.usage_normalization import normalize_usage_tokens
from cognis.models.usage_pricing import (
    matching_untiered_scope,
    normalize_usage_service_tier,
    usage_operation_matches_scope,
)

SCALE = Decimal("0.000000000001")
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class UsagePriceResult:
    """A known subtotal is distinct from a complete valuation or no price."""

    cost: Decimal | None
    status: str
    currency: str | None
    basis: str | None
    units: tuple[tuple[str, int], ...]
    reason: str | None = None


def disjoint_price_contracts(before: UsagePriceDefinition, after: UsagePriceDefinition) -> bool:
    """Legacy concrete-tier and proven untiered rules cannot price the same fact."""
    if before.media_operations or after.media_operations:
        if before.media_operations and after.media_operations:
            return not set(before.media_operations) & set(after.media_operations)
        # Legacy concrete-tier tariffs could already price media submissions.
        return False
    return (before.untiered_tariff is None) != (after.untiered_tariff is None)


def valuation_domain(
    historical: UsagePriceResult,
    current: UsagePriceResult,
) -> tuple[str | None, str | None]:
    """An ineligible current tariff must not relabel a booked historical amount."""
    if current.cost is None and historical.cost is not None:
        return historical.currency, historical.basis
    return current.currency or historical.currency, current.basis or historical.basis


def billable_units(
    usage: NormalizedUsage,
    selection: UsageModelSelection | None = None,
    *,
    prices: UsagePriceDefinition | None = None,
) -> tuple[dict[str, int], bool]:
    """Never add inclusive cache/reasoning subsets twice or infer unknown counts."""
    evidence = selection.service_tier_applicability if selection is not None else None
    aggregate_scope = (
        selection is not None
        and evidence is not None
        and evidence.state == "not_applicable"
        and evidence.scope is not None
        and usage_operation_matches_scope(selection.operation, evidence.scope)
        and selection.service_tier is None
        and selection.effective_parameters.service_tier is None
    )
    native_media = (
        bool(prices.media_operations)
        if prices is not None
        else (
            selection is not None
            and selection.operation
            in {
                "image_generate",
                "image_edit",
                "transcribe",
                "synthesize",
                "video_generate",
                "video_extend",
            }
            and not aggregate_scope
        )
    )
    if native_media:
        media_units: dict[str, int] = {item.name: item.value for item in usage.extras}
        for name in ("input", "output", "cache_read"):
            counter = getattr(usage, f"{name}_tokens")
            if counter.value is not None:
                media_units[name] = counter.value
        return media_units, usage.completeness == "reported" and all(
            item.status != "partial" for item in usage.extras
        )
    if aggregate_scope and evidence is not None and evidence.scope is not None:
        dimensions: tuple[str, ...] = ("input",)
        if evidence.scope.resource == "openai.images.generate":
            dimensions += ("output",)
        aggregate_units = {}
        complete = not usage.extras
        for dimension in dimensions:
            counter = getattr(usage, f"{dimension}_tokens")
            if counter.value is not None:
                aggregate_units[dimension] = counter.value
            complete &= counter.value is not None and counter.status == "reported"
        return aggregate_units, bool(complete)
    units: dict[str, int] = {}
    complete = usage.completeness == "reported" and not usage.extras
    normalized = normalize_usage_tokens(usage)
    for key in type(normalized.billable).model_fields:
        count = getattr(normalized.billable, key)
        if count.value is not None:
            units[key] = count.value
            complete &= count.status in {"reported", "derived"}
        elif count.status != "not_applicable":
            complete = False
    return units, bool(complete)


def applicable_price_rates(
    prices: UsagePriceDefinition,
    service_tier: str | None,
    *,
    selection: UsageModelSelection | None = None,
) -> tuple[UsagePriceRate, ...]:
    """Require known dispatch evidence and an explicitly matching tariff."""
    if prices.media_operations:
        if selection is None or selection.operation not in prices.media_operations:
            return ()
        return tuple(
            rate
            for rate in prices.rates
            if rate.image_size in (None, selection.effective_parameters.image_size)
            and rate.image_quality in (None, selection.effective_parameters.image_quality)
        )
    if prices.untiered_tariff is not None:
        return (
            tuple(rate for rate in prices.rates if rate.service_tier is None)
            if selection is not None and matching_untiered_scope(selection, prices.untiered_tariff)
            else ()
        )
    if (
        selection is not None
        and selection.service_tier_applicability is not None
        and selection.service_tier_applicability.state in {"unknown", "not_applicable"}
    ):
        return ()
    tier = normalize_usage_service_tier(service_tier)
    if tier is None:
        return ()
    return tuple(
        rate
        for rate in prices.rates
        if rate.service_tier is not None and normalize_usage_service_tier(rate.service_tier) == tier
    )


def price_rate(
    prices: UsagePriceDefinition,
    dimension: str,
    *,
    service_tier: str | None,
    cache_ttl: str | None,
    input_tokens: int | None,
    selection: UsageModelSelection | None = None,
) -> UsagePriceRate | None:
    """Select one declared rule; unknown prompt size cannot pick a context tier."""
    cache_ttl = (
        "5m"
        if dimension == "cache_write_5m"
        else "1h"
        if dimension == "cache_write_1h"
        else cache_ttl
    )
    candidates = [
        rate
        for rate in applicable_price_rates(prices, service_tier, selection=selection)
        if rate.dimension == dimension and rate.cache_ttl in (None, cache_ttl)
    ]
    if input_tokens is None and any(rate.input_threshold > 0 for rate in candidates):
        return None
    candidates = [rate for rate in candidates if rate.input_threshold <= (input_tokens or 0)]
    if not candidates:
        return None
    # Provider-specific overrides take precedence, then the highest matching tier.
    return max(
        candidates,
        key=lambda rate: (
            rate.service_tier is not None,
            rate.cache_ttl is not None,
            rate.input_threshold,
        ),
    )


def calculate_price(
    usage: NormalizedUsage,
    selection: UsageModelSelection,
    prices: UsagePriceDefinition | None,
) -> UsagePriceResult:
    """Return reproducible fixed-point estimates; missing price is backfillable."""
    units, complete = billable_units(usage, selection, prices=prices)
    if usage.provider_cost is not None:
        return UsagePriceResult(
            usage.provider_cost.amount.quantize(SCALE),
            "complete",
            usage.provider_cost.currency,
            "provider_reported",
            tuple(sorted(units.items())),
        )
    if prices is None:
        return UsagePriceResult(
            None, "unpriced", None, None, tuple(sorted(units.items())), "missing_price"
        )
    if (prices.provider_id, prices.model) != (selection.provider_id, selection.effective_model):
        raise ValueError("Price revision belongs to a different provider/model")
    if not applicable_price_rates(prices, selection.service_tier, selection=selection):
        return UsagePriceResult(
            None,
            "unpriced",
            prices.currency,
            prices.basis,
            tuple(sorted(units.items())),
            "unknown_service_tier_or_missing_rule",
        )
    if prices.media_operations:
        # Only the tariff's declared dimensions are billable. Display counts are
        # evidence, not additional charges beside token or duration tariffs.
        rates = applicable_price_rates(prices, selection.service_tier, selection=selection)
        required = {rate.dimension for rate in rates}
        extras: dict[str, UsageNumericExtra] = {item.name: item for item in usage.extras}
        for direction in ("input", "output"):
            text = f"{direction}_text_tokens"
            subsets = [f"{direction}_{kind}_tokens" for kind in ("image", "audio")]
            if direction in required and any(name in required for name in subsets):
                if text in units:
                    units[direction] = units[text]
                elif direction in units and all(
                    name in units for name in subsets if name in required
                ):
                    units[direction] -= sum(units[name] for name in subsets if name in required)
                    if units[direction] < 0:
                        units.pop(direction)
                else:
                    units.pop(direction, None)
        if {"input", "cache_read"} <= required and {"input", "cache_read"} <= units.keys():
            cached_modalities = [
                f"cache_read_{kind}_tokens"
                for kind in ("image", "audio")
                if f"input_{kind}_tokens" in required
            ]
            if all(name in units for name in cached_modalities):
                units["cache_read"] -= sum(units[name] for name in cached_modalities)
                units["input"] -= units["cache_read"]
                if units["cache_read"] < 0 or units["input"] < 0:
                    units.pop("input")
                    units.pop("cache_read")
            else:
                # Aggregate cached input cannot identify the cached text share.
                units.pop("input")
                units.pop("cache_read")
        elif {"input", "cache_read"} <= required:
            units.pop("input", None)
        for kind in ("image", "audio"):
            name, cached = f"input_{kind}_tokens", f"cache_read_{kind}_tokens"
            if {name, cached} <= required and {name, cached} <= units.keys():
                units[name] -= units[cached]
                if units[name] < 0:
                    units.pop(name)
            elif {name, cached} <= required:
                units.pop(name, None)
        complete = (
            all(
                name in units and (name not in extras or extras[name].status != "partial")
                for name in required
            )
            and usage.completeness != "partial"
        )
        units = {name: value for name, value in units.items() if name in required}
    total = Decimal(0)
    priced = False
    input_total = normalize_usage_tokens(usage).input.value
    for dimension, value in units.items():
        if value == 0:
            continue
        rate = price_rate(
            prices,
            dimension,
            service_tier=selection.service_tier,
            cache_ttl=selection.cache_ttl,
            input_tokens=input_total,
            selection=selection,
        )
        if rate is None:
            complete = False
            continue
        priced = True
        total += Decimal(value) * rate.per_million / MILLION
    if not priced and (not complete or any(units.values())):
        return UsagePriceResult(
            None,
            "unpriced",
            prices.currency,
            prices.basis,
            tuple(sorted(units.items())),
            "unknown_units_or_missing_rule",
        )
    return UsagePriceResult(
        total.quantize(SCALE),
        "complete" if complete else "partial",
        prices.currency,
        prices.basis,
        tuple(sorted(units.items())),
    )
