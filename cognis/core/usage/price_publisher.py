"""Off-inference metadata publication and atomically replaced committed price pins."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.models.config import ModelInfo
from cognis.models.usage import (
    UsagePriceDefinition,
    UsagePriceRate,
    UsageServiceTierApplicability,
)
from cognis.models.usage_pricing import (
    UsagePricingPin,
    normalize_usage_service_tier,
    usage_pricing_fingerprint,
)
from cognis.providers.llm.inference_accounting import pricing_snapshot
from cognis.store.models import LLMProvider
from cognis.store.usage.transactions import notify_usage

logger = logging.getLogger(__name__)
MAX_PRICE_PINS = 512
PUBLICATION_RETRY_SECONDS = 60.0
_DIMENSIONS: dict[str, Literal["input", "output", "cache_read", "cache_write"]] = {
    "input_cost_per_mtok": "input",
    "output_cost_per_mtok": "output",
    "cache_read_cost_per_mtok": "cache_read",
    "cache_write_cost_per_mtok": "cache_write",
}
_NATIVE_PRICES: dict[
    str, Literal["input", "output", "cache_read", "cache_write_5m", "cache_write_1h"]
] = {
    "input_cost_per_token": "input",
    "output_cost_per_token": "output",
    "cache_read_input_token_cost": "cache_read",
    "cache_creation_input_token_cost": "cache_write_5m",
    "cache_creation_input_token_cost_1hr": "cache_write_1h",
    "cache_creation_input_token_cost_above_1hr": "cache_write_1h",
    "prompt": "input",
    "completion": "output",
    "input_cache_read": "cache_read",
}
# Rates for tiers a dispatch never selects implicitly; they cannot price it.
_OTHER_TIER_PRICE = re.compile(r"_(batches|flex|priority|ultrafast)\Z")
_CONTEXT_PRICE = re.compile(
    r"(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost|"
    r"cache_creation_input_token_cost)_above_(\d+)(k?)_tokens\Z"
)


@dataclass(frozen=True)
class UsagePricePublisherDiagnostics:
    """Content-free cache/publication coverage and supervised worker health."""

    running: bool
    cached_pins: int
    unsupported_models: int
    truncated: bool
    paused_error_code: str | None
    resolution_failures: int = 0
    resolution_error_code: str | None = None


class UsagePricePinCache:
    """Readers see one complete bounded immutable snapshot, never prepared SQL intent."""

    def __init__(self) -> None:
        self._pins: Mapping[tuple[str, str, str], UsagePricingPin] = MappingProxyType({})

    def __call__(
        self, *, provider_id: str, model: str, metadata_fingerprint: str
    ) -> UsagePricingPin | None:
        """Synchronous exact match only; a miss has no I/O or publication side effect."""
        return self._pins.get((provider_id, model, metadata_fingerprint))

    def replace(self, pins: Mapping[tuple[str, str, str], UsagePricingPin]) -> None:
        """Publication owner supplies committed pins only, after successful transactions."""
        if len(pins) > MAX_PRICE_PINS:
            raise ValueError("Usage price cache exceeds its entry budget")
        self._pins = MappingProxyType(dict(pins))

    def invalidate(self) -> None:
        """Immediately fail unknown until the next complete committed snapshot is ready."""
        self._pins = MappingProxyType({})

    def __len__(self) -> int:
        return len(self._pins)


def metadata_price(
    *, provider_id: str, model: str, info: ModelInfo, config: dict[str, Any], now: datetime
) -> tuple[str, UsagePriceDefinition | None]:
    """Compile only provenance-backed declared rates; unsupported rules stay unpriced."""
    if info.untiered_tariff is None and info.model_mode in {
        "image_generation",
        "audio_speech",
        "audio_transcription",
        "video",
    }:
        return media_metadata_price(
            provider_id=provider_id, model=model, info=info, config=config, now=now
        )
    declaration = info.untiered_tariff
    if declaration is not None:
        evidence = UsageServiceTierApplicability(state="not_applicable", scope=declaration.scope)
        fingerprint = usage_pricing_fingerprint(
            provider_id=provider_id,
            model=model,
            model_info=info,
            provider_config=config,
            applicability=evidence,
        )
        sources = info.provider_metadata.get("field_sources", {})
        configured = next(
            (
                entry
                for entry in config.get("models", [])
                if isinstance(entry, dict) and entry.get("model_id") in (model, info.model_id)
            ),
            {},
        )
        if (
            not isinstance(sources, dict)
            or sources.get("untiered_tariff") != "configured"
            or configured.get("untiered_tariff") != declaration.model_dump(mode="json")
            or configured.get("pricing")
            or configured.get("cache_read_cost_per_mtok") is not None
            or configured.get("cache_write_cost_per_mtok") is not None
        ):
            return fingerprint, None
        fields: tuple[str, ...] = ("input_cost_per_mtok",)
        if declaration.billing_basis == "aggregate_input_output_tokens":
            fields += ("output_cost_per_mtok",)
        rates = []
        for field in fields:
            value = getattr(info, field)
            if (
                sources.get(field) != "configured"
                or configured.get(field) != value
                or isinstance(value, bool)
                or not isinstance(value, int | float)
            ):
                return fingerprint, None
            amount = Decimal(str(value))
            if not amount.is_finite() or amount < 0:
                return fingerprint, None
            rates.append(UsagePriceRate(dimension=_DIMENSIONS[field], per_million=amount))
        return fingerprint, UsagePriceDefinition(
            revision_id="upr_v3_" + fingerprint[4:],
            source_revision=fingerprint,
            provider_id=provider_id,
            model=model,
            effective_at=now,
            recorded_at=now,
            currency="USD",
            basis="configured_estimate",
            provenance="configured",
            untiered_tariff=declaration,
            rule_version=2,
            rates=tuple(rates),
        )
    fingerprint = usage_pricing_fingerprint(
        provider_id=provider_id,
        model=model,
        model_info=info,
        provider_config=config,
        service_tier=info.tier,
    )
    snapshot = pricing_snapshot(info)
    if snapshot is None:
        return fingerprint, None
    rates = []
    field_sources = info.provider_metadata.get("field_sources", {})
    # Capture bills a request that names no tier at this same default tier.
    service_tier = normalize_usage_service_tier(info.tier)
    if service_tier is None:
        return fingerprint, None
    for field, value in snapshot["rates"].items():
        native_cache = (
            field == "cache_write_cost_per_mtok" and snapshot["sources"][field] != "configured"
        )
        rates.append(
            UsagePriceRate(
                dimension=_DIMENSIONS[field],
                per_million=Decimal(str(value)),
                cache_ttl="5m" if native_cache else None,
                service_tier=service_tier,
            )
        )
    sources = set(snapshot["sources"].values())
    token_priced = any(
        not _is_zero_price(snapshot["rates"].get(field))
        for field in ("input_cost_per_mtok", "output_cost_per_mtok")
        if field in snapshot["rates"]
    )
    pricing_source = field_sources.get("pricing") if isinstance(field_sources, dict) else None
    if isinstance(pricing_source, str) and (
        pricing_source in {"provider", "configured"} or pricing_source.startswith("catalog:")
    ):
        for name, value in info.pricing.items():
            if _OTHER_TIER_PRICE.search(name) or _is_zero_price(value):
                continue
            if name in _DIMENSIONS or name in _NATIVE_PRICES:
                if name in _NATIVE_PRICES and _NATIVE_PRICES[name].startswith("cache_write_"):
                    rates.append(
                        UsagePriceRate(
                            dimension=_NATIVE_PRICES[name],
                            per_million=Decimal(str(value)) * 1_000_000,
                            service_tier=service_tier,
                        )
                    )
                    if _NATIVE_PRICES[name] == "cache_write_1h":
                        rates.append(
                            UsagePriceRate(
                                dimension="cache_write",
                                cache_ttl="1h",
                                per_million=Decimal(str(value)) * 1_000_000,
                                service_tier=service_tier,
                            )
                        )
                continue
            match = _CONTEXT_PRICE.fullmatch(name)
            if match is None:
                # Token-priced models ignore extras the estimate does not meter
                # (search, images, longer cache retention), as LiteLLM does.
                # Models without a token rate stay unpriced instead of zero.
                # Tiered rules change the token price itself; never ignore those.
                if token_priced and name != "tiered_pricing":
                    continue
                return fingerprint, None
            base, threshold, kilo = match.groups()
            inclusive_threshold = int(threshold) * (1000 if kilo else 1) + 1
            rates.append(
                UsagePriceRate(
                    dimension=_NATIVE_PRICES[base],
                    per_million=Decimal(str(value)) * 1_000_000,
                    input_threshold=inclusive_threshold,
                    service_tier=service_tier,
                )
            )
            if base == "cache_creation_input_token_cost":
                rates.append(
                    UsagePriceRate(
                        dimension="cache_write",
                        cache_ttl="5m",
                        per_million=Decimal(str(value)) * 1_000_000,
                        input_threshold=inclusive_threshold,
                        service_tier=service_tier,
                    )
                )
    elif info.pricing:
        # An unproven tier/price must not silently become a complete flat estimate.
        return fingerprint, None
    provenance: Literal["configured", "provider_metadata", "catalog"] = (
        "configured"
        if "configured" in sources
        else "provider_metadata"
        if "provider" in sources
        else "catalog"
    )
    return fingerprint, UsagePriceDefinition(
        # Default-tier applicability changed in compiler v4; never overwrite an
        # older revision produced from the same immutable metadata fingerprint.
        revision_id="upr_v4_" + fingerprint[4:],
        source_revision=fingerprint,
        provider_id=provider_id,
        model=model,
        effective_at=now,
        recorded_at=now,
        currency="USD",
        basis=(
            "configured_estimate"
            if config.get("preset") in {"ollama", "llamacpp", "vllm", "custom"}
            else "api_equivalent"
        ),
        provenance=provenance,
        rates=tuple(rates),
    )


def _is_zero_price(value: Any) -> bool:
    """A free extra dimension cannot change an estimate; it never blocks pricing."""
    try:
        return Decimal(str(value)) == 0
    except ArithmeticError:
        return False


def media_metadata_price(
    *, provider_id: str, model: str, info: ModelInfo, config: dict[str, Any], now: datetime
) -> tuple[str, UsagePriceDefinition | None]:
    """Compile one native billing family; alternate approximate prices are not additive."""
    fingerprint = usage_pricing_fingerprint(
        provider_id=provider_id, model=model, model_info=info, provider_config=config
    )
    source = info.provider_metadata.get("field_sources", {}).get("pricing")
    if not isinstance(source, str) or not (
        source in {"configured", "provider"} or source.startswith("catalog:")
    ):
        return fingerprint, None
    pricing = info.pricing
    assert info.model_mode is not None
    operations = {
        "image_generation": ("image_generate", "image_edit"),
        "audio_transcription": ("transcribe",),
        "audio_speech": ("synthesize",),
        "video": ("video_generate", "video_extend"),
    }[info.model_mode]
    mapping: dict[str, tuple[str, int]] = {}
    if info.model_mode == "image_generation" and "output_cost_per_image" in pricing:
        mapping = {
            "input_cost_per_image": ("input_image_count", 1_000_000),
            "output_cost_per_image": ("output_image_count", 1_000_000),
        }
    elif info.model_mode == "audio_speech" and "input_cost_per_character" in pricing:
        mapping = {"input_cost_per_character": ("input_characters", 1_000_000)}
    elif info.model_mode == "audio_transcription" and not any(
        key in pricing for key in ("input_cost_per_token", "input_cost_per_audio_token")
    ):
        mapping = {"input_cost_per_second": ("input_audio_duration_ms", 1000)}
    elif info.model_mode == "video":
        mapping = {"output_cost_per_second": ("output_video_duration_ms", 1000)}
    else:
        mapping = {
            "input_cost_per_token": ("input", 1_000_000),
            "output_cost_per_token": ("output", 1_000_000),
            "input_cost_per_image_token": ("input_image_tokens", 1_000_000),
            "output_cost_per_image_token": ("output_image_tokens", 1_000_000),
            "input_cost_per_audio_token": ("input_audio_tokens", 1_000_000),
            "output_cost_per_audio_token": ("output_audio_tokens", 1_000_000),
            "cache_read_input_token_cost": ("cache_read", 1_000_000),
            "cache_read_input_image_token_cost": ("cache_read_image_tokens", 1_000_000),
            "cache_read_input_audio_token_cost": ("cache_read_audio_tokens", 1_000_000),
        }
        # Some snapshots also expose an aggregate output approximation beside
        # the actual audio/image token price.
        if any(
            key in pricing for key in ("output_cost_per_image_token", "output_cost_per_audio_token")
        ):
            mapping.pop("output_cost_per_token")
    rates = []
    for name, (dimension, scale) in mapping.items():
        value = pricing.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return fingerprint, None
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return fingerprint, None
        rates.append(
            UsagePriceRate.model_validate(
                {
                    "dimension": dimension,
                    "per_million": amount * scale,
                }
            )
        )
    # Explicit scalar overrides win over the same catalog token dimension.
    sources = info.provider_metadata.get("field_sources", {})
    for field, dimension in _DIMENSIONS.items():
        value = getattr(info, field)
        if sources.get(field) == "configured" and value is not None:
            rates = [rate for rate in rates if rate.dimension != dimension]
            rates.append(UsagePriceRate(dimension=dimension, per_million=Decimal(str(value))))
    if not rates:
        return fingerprint, None
    return fingerprint, UsagePriceDefinition(
        revision_id="upr_media_v1_" + fingerprint[4:],
        source_revision=fingerprint,
        provider_id=provider_id,
        model=model,
        effective_at=now,
        recorded_at=now,
        currency="USD",
        basis="configured_estimate"
        if config.get("preset") in {"ollama", "llamacpp", "vllm", "custom"}
        else "api_equivalent",
        provenance="configured"
        if source == "configured"
        else "provider_metadata"
        if source == "provider"
        else "catalog",
        rule_version=3,
        media_operations=operations,
        rates=tuple(rates),
    )


class UsagePricePublisher:
    """One event-driven warmer; no recurring metadata refresh or inference-time SQL."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        maintenance: UsageMaintenanceService,
        cache: UsagePricePinCache,
        resolve_model: Callable[..., Awaitable[ModelInfo]],
    ) -> None:
        self.sessions = sessions
        self.maintenance = maintenance
        self.cache = cache
        self.resolve_model = resolve_model
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._generation = 0
        self._unsupported = 0
        self._truncated = False
        self._paused: str | None = None
        self._resolution_failures = 0
        self._resolution_error_code: str | None = None

    @property
    def diagnostics(self) -> UsagePricePublisherDiagnostics:
        """Expose unsupported sources, bounded truncation and task/publication failures."""
        return UsagePricePublisherDiagnostics(
            self._task is not None and not self._task.done(),
            len(self.cache),
            self._unsupported,
            self._truncated,
            self._paused,
            self._resolution_failures,
            self._resolution_error_code,
        )

    def wake(self) -> None:
        """Schedule a refresh after a config save or reconnect.

        Committed pins stay readable meanwhile: they are keyed by the price
        fingerprint, so a changed price-relevant configuration cannot match an
        old pin, while unrelated saves (for example credential refreshes) do
        not leave requests unpriced until the refresh finishes.
        """
        if not self._closing:
            self._generation += 1
            self._idle.clear()
            self._wake.set()

    async def notify_configuration(self, session: AsyncSession) -> None:
        """Publish a coalescible identity-free HA hint in the configuration transaction."""
        await notify_usage(
            session,
            self.maintenance.controller_owner_id,
            f"upconfig_{uuid.uuid4().hex}",
            kind="usage_pricing_changed",
        )

    async def start(self) -> None:
        """Warm once after normalized model metadata and schema services are available."""
        if self._closing:
            raise RuntimeError("Usage price publisher is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-price-publisher")
            self._task.add_done_callback(self._done)
            self.wake()

    def _done(self, task: asyncio.Task[None]) -> None:
        if not self._closing:
            error = None if task.cancelled() else task.exception()
            self._paused = type(error).__name__ if error else "UnexpectedStop"
            self.cache.invalidate()
            self._idle.set()
            logger.warning("Usage price publisher stopped: %s", self._paused)

    async def drain(self, *, timeout_seconds: float = 5) -> bool:
        """Off-inference startup/test barrier; never starts another publication cycle."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout_seconds)
        return self._idle.is_set() and self._paused is None

    async def _run(self) -> None:
        while True:
            if self._paused is None:
                await self._wake.wait()
            else:
                # A failed publication retries on its own; it must not wait for
                # the next configuration change to price new requests.
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), PUBLICATION_RETRY_SECONDS)
                if not self._wake.is_set():
                    self._generation += 1
            self._wake.clear()
            self._idle.clear()
            generation = self._generation
            self._paused = None
            try:
                await self.refresh(generation=generation)
            except Exception as exc:
                # Committed pins stay valid; only this refresh is abandoned.
                self._paused = type(exc).__name__
                logger.warning("Usage price publication paused: %s", self._paused)
            if not self._wake.is_set():
                self._idle.set()

    async def refresh(self, *, generation: int | None = None) -> None:
        """Resolve without a session; publish each small revision before exposing pins."""
        generation = self._generation if generation is None else generation
        pins: dict[tuple[str, str, str], UsagePricingPin] = {}
        unsupported = 0
        truncated = False
        cursor: str | None = None
        examined = 0
        providers = 0
        resolution_failures = 0
        resolution_error_code: str | None = None
        while examined < MAX_PRICE_PINS and providers < MAX_PRICE_PINS:
            async with self.sessions() as session:
                statement = select(LLMProvider).order_by(LLMProvider.provider_id).limit(1)
                if cursor is not None:
                    statement = statement.where(LLMProvider.provider_id > cursor)
                row = await session.scalar(statement)
                if row is None:
                    break
                providers += 1
                cursor = row.provider_id
                provider_id, actor = row.provider_id, row.owner_email
                config, revision, status = dict(row.config), row.updated_at, row.status
            if status != "active":
                continue
            configured = config.get("models", [])
            truncated |= isinstance(configured, list) and len(configured) > MAX_PRICE_PINS
            models = (
                {
                    item["model_id"]
                    for item in configured[:MAX_PRICE_PINS]
                    if isinstance(item, dict) and isinstance(item.get("model_id"), str)
                }
                if isinstance(configured, list)
                else set()
            )
            if isinstance(config.get("default_model"), str):
                models.add(config["default_model"])
            for model in sorted(models):
                if generation != self._generation or self._closing:
                    return
                if examined == MAX_PRICE_PINS:
                    truncated = True
                    break
                examined += 1
                try:
                    async with asyncio.timeout(5):
                        info = await self.resolve_model(
                            model, provider_id=provider_id, acting_user_email=actor
                        )
                except Exception as exc:
                    # Optional per-model metadata failure cannot evict unrelated
                    # committed prices. Cancellation still belongs to the worker.
                    resolution_failures += 1
                    resolution_error_code = type(exc).__name__
                    logger.warning("Usage model metadata unavailable: %s", resolution_error_code)
                    continue
                try:
                    fingerprint, definition = metadata_price(
                        provider_id=provider_id,
                        model=model,
                        info=info,
                        config=config,
                        now=datetime.now(UTC),
                    )
                except ValueError:
                    unsupported += 1
                    continue
                if definition is None:
                    unsupported += 1
                    continue
                await self.maintenance.publish_price(
                    definition,
                    expected_provider_config=config,
                    expected_provider_revision=revision,
                )
                pins[provider_id, model, fingerprint] = UsagePricingPin(
                    captured_price_revision_id=definition.revision_id,
                    resolver_snapshot_id=fingerprint,
                )
                await asyncio.sleep(0)
        if generation == self._generation and not self._closing:
            self.cache.replace(pins)
            self._unsupported = unsupported
            self._truncated = truncated or examined == MAX_PRICE_PINS or providers == MAX_PRICE_PINS
            self._resolution_failures = resolution_failures
            self._resolution_error_code = resolution_error_code

    async def aclose(self) -> None:
        """Cancel only this worker, leaving committed revisions and projection intent durable."""
        self._closing = True
        self.cache.invalidate()
        if self._task is not None:
            self._task.cancel()
            done, _ = await asyncio.wait({self._task}, timeout=5)
            if done:
                with suppress(asyncio.CancelledError):
                    self._task.result()
                self._task = None
            else:
                self._paused = "ShutdownTimeout"
                logger.warning("Usage price publisher shutdown paused: ShutdownTimeout")
