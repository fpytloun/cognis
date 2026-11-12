"""Publisher/capture share exact metadata identity without credential exposure."""

from collections.abc import Iterator
from typing import Any

import pytest

from cognis.models.config import ModelInfo
from cognis.models.usage_pricing import (
    UsagePricingPin,
    normalize_usage_service_tier,
    usage_pricing_fingerprint,
)


def fingerprint(info: ModelInfo, **config: object) -> str:
    return usage_pricing_fingerprint(
        provider_id="openai", model="gpt-4.1", model_info=info, provider_config=config
    )


def test_price_and_provenance_are_part_of_snapshot_identity() -> None:
    info = ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2)
    baseline = fingerprint(info)
    assert fingerprint(info.model_copy(update={"input_cost_per_mtok": 3})) != baseline
    assert (
        fingerprint(
            info.model_copy(
                update={
                    "provider_metadata": {"field_sources": {"input_cost_per_mtok": "configured"}}
                }
            )
        )
        != baseline
    )
    assert fingerprint(info, models=[{"model_id": "gpt-4.1", "input_cost_per_mtok": 3}]) != baseline


def test_credentials_payloads_and_capabilities_are_excluded() -> None:
    info = ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2)
    changed = info.model_copy(
        update={
            "context_window": 128000,
            "provider_metadata": {"api_key": "private"},
            "runtime_metadata": {"response_headers": {"authorization": "private"}},
        }
    )
    assert fingerprint(info) == fingerprint(
        changed, api_key="private", headers={"authorization": "private"}
    )
    assert fingerprint(
        info, models=[{"model_id": "other", "input_cost_per_mtok": 99}]
    ) == fingerprint(info)


def test_fingerprint_is_bounded_and_rejects_nonfinite_prices() -> None:
    for pricing in ({"rate": float("nan")}, {"rate": "x" * 513}, {"rates": [1] * 65}):
        with pytest.raises(ValueError):
            fingerprint(ModelInfo(model_id="gpt-4.1", pricing=pricing))


def test_pin_is_immutable_and_has_explicit_unknown_price() -> None:
    pin = UsagePricingPin(captured_price_revision_id=None, resolver_snapshot_id="upm_snapshot")
    assert pin.captured_price_revision_id is None
    with pytest.raises(AttributeError):
        pin.resolver_snapshot_id = "changed"  # type: ignore[misc]


def test_actual_dispatch_tier_cannot_reuse_standard_metadata_pin() -> None:
    info = ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2)
    assert usage_pricing_fingerprint(
        provider_id="openai",
        model="gpt-4.1",
        model_info=info,
        provider_config={},
        service_tier="priority",
    ) != fingerprint(info)


@pytest.mark.parametrize("match_index", [0, 256, None])
def test_oversized_source_is_rejected_without_traversal(match_index: int | None) -> None:
    class UntraversableModels(list[dict[str, Any]]):
        def __iter__(self) -> Iterator[dict[str, Any]]:
            raise AssertionError("Oversized model sources must not be traversed")

    models = UntraversableModels([{"model_id": f"other-{index}"} for index in range(257)])
    if match_index is not None:
        models[match_index] = {"model_id": "gpt-4.1", "input_cost_per_mtok": 3}
    with pytest.raises(ValueError, match="source budget"):
        fingerprint(ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2), models=models)


def test_source_budget_boundary_preserves_late_exact_hit() -> None:
    info = ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2)
    override = {"model_id": info.model_id, "input_cost_per_mtok": 3, "api_key": "private"}
    models = [{"model_id": f"other-{index}"} for index in range(255)] + [override]
    assert fingerprint(info, models=models) == fingerprint(info, models=[override])
    assert fingerprint(info, models=models) != fingerprint(info)
    assert fingerprint(info, models=models) == fingerprint(
        info, models=[{**override, "api_key": "different"}]
    )


@pytest.mark.parametrize("models", [None, (), {"gpt-4.1": {"input_cost_per_mtok": 3}}])
def test_unsupported_source_shape_does_not_prove_an_absent_override(models: object) -> None:
    with pytest.raises(ValueError, match="source budget"):
        fingerprint(ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2), models=models)


@pytest.mark.parametrize("wire_tier", [None, "auto", "unknown", "AUTO", " "])
def test_unknown_tier_is_distinct_from_the_standard_metadata_default(wire_tier: str | None) -> None:
    info = ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2)
    assert info.tier == "standard"
    assert normalize_usage_service_tier(wire_tier) is None
    assert usage_pricing_fingerprint(
        provider_id="openai",
        model=info.model_id,
        model_info=info,
        provider_config={},
        service_tier=wire_tier,
    ) != usage_pricing_fingerprint(
        provider_id="openai",
        model=info.model_id,
        model_info=info,
        provider_config={},
        service_tier="standard",
    )


def test_explicit_native_default_label_and_tier_provenance_share_one_identity_rule() -> None:
    info = ModelInfo(model_id="gpt-4.1", input_cost_per_mtok=2)
    assert normalize_usage_service_tier("default") == "standard"
    assert usage_pricing_fingerprint(
        provider_id="openai",
        model=info.model_id,
        model_info=info,
        provider_config={},
        service_tier="default",
    ) == usage_pricing_fingerprint(
        provider_id="openai",
        model=info.model_id,
        model_info=info,
        provider_config={},
        service_tier="standard",
    )
    explicit = info.model_copy(
        update={"provider_metadata": {"field_sources": {"tier": "configured"}}}
    )
    assert fingerprint(explicit) != fingerprint(info)
