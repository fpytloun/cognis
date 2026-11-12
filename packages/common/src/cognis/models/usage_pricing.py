"""Pure snapshot identity and nonblocking pricing lookup shared by capture and publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import ValidationError

from cognis.models.config import ModelInfo
from cognis.models.usage import (
    UsageModelSelection,
    UsageOperation,
    UsageOperationScope,
    UsageServiceTierApplicability,
    UsageUntieredTariffDeclaration,
)

PRICE_FIELDS = (
    "input_cost_per_mtok",
    "output_cost_per_mtok",
    "cache_read_cost_per_mtok",
    "cache_write_cost_per_mtok",
)
_METADATA_FIELDS = (*PRICE_FIELDS, "pricing", "model_mode", "tier", "primary_cache_ttl")
_CONFIG_FIELDS = ("backend", "executor_backend", "litellm_provider", "sdk", "preset")
_MAX_CONFIGURED_MODELS = 256


@dataclass(frozen=True, slots=True)
class UsagePricingPin:
    """Only committed immutable identities; a missing price is never zero."""

    captured_price_revision_id: str | None
    resolver_snapshot_id: str


@dataclass(frozen=True, slots=True)
class UsageExecutorPriceCandidate:
    """One dispatch-frozen candidate; never supplied by executor or caller metadata."""

    provider_id: str
    model: str
    scope: UsageOperationScope
    fingerprint: str
    pin: UsagePricingPin | None


class UsagePricingLookup(Protocol):
    """Memory-only lookup: never SQL, await, capacity wait, or metadata publication."""

    def __call__(
        self, *, provider_id: str, model: str, metadata_fingerprint: str
    ) -> UsagePricingPin | None:
        """Return None on cold, unsupported, stale, or mismatched metadata."""
        ...


def _bounded(value: Any, budget: list[int], depth: int = 0) -> Any:
    budget[0] -= 1
    if budget[0] < 0 or depth > 6:
        raise ValueError("Pricing metadata exceeds the fingerprint budget")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        number = Decimal(str(value))
        if not number.is_finite() or abs(number.adjusted()) > 36:
            raise ValueError("Pricing metadata contains an unsupported number")
        return {"number": format(number.normalize(), "f")}
    if isinstance(value, str):
        if len(value) > 512:
            raise ValueError("Pricing metadata contains an oversized string")
        return value
    if isinstance(value, Mapping):
        if len(value) > 64 or any(not isinstance(key, str) or len(key) > 128 for key in value):
            raise ValueError("Pricing metadata contains an unsupported mapping")
        return {key: _bounded(item, budget, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple) and len(value) <= 64:
        return [_bounded(item, budget, depth + 1) for item in value]
    raise ValueError("Pricing metadata contains an unsupported value")


def normalize_usage_service_tier(service_tier: str | None) -> str | None:
    """Normalize explicit native labels, never absent/auto applicability evidence."""
    if (
        service_tier is None
        or service_tier != service_tier.strip()
        or service_tier.casefold() in {"", "auto", "unknown"}
    ):
        return None
    return "standard" if service_tier == "default" else service_tier


def configured_untiered_model_info(
    provider_config: Mapping[str, Any],
    model: str,
) -> ModelInfo | None:
    """Project explicit tariff configuration without discovery, SQL or waits."""
    entries = provider_config.get("models", [])
    if not isinstance(entries, list) or len(entries) > _MAX_CONFIGURED_MODELS:
        return None
    entry = next(
        (item for item in entries if isinstance(item, dict) and item.get("model_id") == model),
        None,
    )
    if entry is None or not isinstance(entry.get("untiered_tariff"), dict):
        return None
    try:
        declaration = UsageUntieredTariffDeclaration.model_validate(entry["untiered_tariff"])
        fields: tuple[str, ...] = ("input_cost_per_mtok",)
        if declaration.billing_basis == "aggregate_input_output_tokens":
            fields += ("output_cost_per_mtok",)
        values = {field: entry[field] for field in fields}
        if any(
            isinstance(value, bool) or not isinstance(value, int | float)
            for value in values.values()
        ):
            return None
        return ModelInfo(
            model_id=model,
            untiered_tariff=declaration,
            **values,
            provider_metadata={
                "field_sources": {field: "configured" for field in (*fields, "untiered_tariff")}
            },
        )
    except (ValidationError, KeyError):
        return None


def usage_pricing_fingerprint(
    *,
    provider_id: str,
    model: str,
    model_info: ModelInfo,
    provider_config: Mapping[str, Any],
    service_tier: str | None = None,
    applicability: UsageServiceTierApplicability | None = None,
) -> str:
    """Hash the actual resolved price metadata and its price-relevant configuration.

    Both publisher and physical-target capture must call this function with the
    same resolved snapshot. Credentials, headers, provider/runtime payloads and
    unrelated model capabilities are deliberately excluded. Unsupported metadata
    raises ValueError; capture leaves both pins unknown instead of guessing.
    Configured model lists over 256 entries are rejected before traversal, even
    if an early entry might match. Truncation cannot prove absence of an override.
    """
    sources = model_info.provider_metadata.get("field_sources", {})
    sources = sources if isinstance(sources, dict) else {}
    configured = provider_config.get("models", [])
    if not isinstance(configured, list) or len(configured) > _MAX_CONFIGURED_MODELS:
        raise ValueError("Configured models exceed the fingerprint source budget")
    override: dict[str, Any] = {}
    for entry in configured:
        if isinstance(entry, dict) and entry.get("model_id") in (model, model_info.model_id):
            override = {key: entry[key] for key in _METADATA_FIELDS if key in entry}
            break
    payload = {
        "version": 2,
        "provider_id": provider_id,
        "model": model,
        "service_tier": normalize_usage_service_tier(service_tier),
        "metadata": {key: getattr(model_info, key) for key in _METADATA_FIELDS},
        "sources": {
            key: sources[key] for key in (*PRICE_FIELDS, "pricing", "tier") if key in sources
        },
        "configuration": {key: provider_config.get(key) for key in _CONFIG_FIELDS},
        "override": override,
    }
    # Keep v2 byte identity. The new domain depends only on the explicitly
    # declared aggregate rates, never unrelated catalog or capability defaults.
    if applicability is not None and applicability.state == "not_applicable":
        declaration = model_info.untiered_tariff
        fields: tuple[str, ...] = ("input_cost_per_mtok",)
        if declaration is not None and declaration.billing_basis == "aggregate_input_output_tokens":
            fields += ("output_cost_per_mtok",)
        projected = configured_untiered_model_info(provider_config, model)
        payload = {
            "version": 3,
            "provider_id": provider_id,
            "model": model,
            "service_tier": service_tier,
            "service_tier_applicability": applicability.model_dump(mode="json"),
            "untiered_tariff": declaration.model_dump(mode="json") if declaration else None,
            "metadata": {field: getattr(model_info, field) for field in fields},
            "sources": {field: sources.get(field) for field in (*fields, "untiered_tariff")},
            "configuration": {key: provider_config.get(key) for key in _CONFIG_FIELDS},
            "configured": {
                **{field: getattr(projected, field) for field in fields},
                "untiered_tariff": projected.untiered_tariff.model_dump(mode="json"),
            }
            if projected is not None and projected.untiered_tariff is not None
            else None,
        }
    encoded = json.dumps(
        _bounded(payload, [256]), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(encoded) > 8192:
        raise ValueError("Pricing metadata exceeds the fingerprint byte budget")
    return "upm_" + hashlib.sha256(encoded).hexdigest()


def usage_operation_matches_scope(operation: UsageOperation, scope: UsageOperationScope) -> bool:
    """Keep the registered legacy image ABI valid for retained explicit tariffs."""
    return scope.operation == operation or (
        operation == "image_generate"
        and scope.operation == "native"
        and scope.resource == "openai.images.generate"
    )


def matching_untiered_scope(
    selection: UsageModelSelection,
    declaration: UsageUntieredTariffDeclaration | None,
) -> bool:
    """Require independent, exact operation and tariff evidence; null is not proof."""
    evidence = selection.service_tier_applicability
    return bool(
        declaration is not None
        and evidence is not None
        and evidence.state == "not_applicable"
        and evidence.scope == declaration.scope
        and evidence.scope is not None
        and usage_operation_matches_scope(selection.operation, evidence.scope)
        and selection.service_tier is None
        and selection.effective_parameters.service_tier is None
    )
