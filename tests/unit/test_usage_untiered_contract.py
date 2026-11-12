"""No-tier evidence, tariff identity and unknown-unit fail-closed regressions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from cognis.core.usage.price_publisher import UsagePricePinCache, metadata_price
from cognis.core.usage.pricing import calculate_price
from cognis.executor.usage_observer import executor_observer
from cognis.models.config import ModelInfo
from cognis.models.usage import (
    UsageAttemptFinalized,
    UsageModelSelection,
    UsageOperationScope,
    UsageServiceTierApplicability,
    UsageUntieredTariffDeclaration,
)
from cognis.models.usage_pricing import (
    UsagePricingPin,
    configured_untiered_model_info,
    usage_pricing_fingerprint,
)
from cognis.providers.llm.usage_binding import UsageBinding, observed_sdk
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.llm.usage_executor import executor_metadata, ingest_executor
from cognis.providers.llm.usage_normalization import UsageAccumulator
from tests.unit.test_usage_capture_runtime import Sink, request


def tariff(resource: str = "openai.embeddings.create") -> tuple[ModelInfo, dict[str, Any]]:
    declaration = UsageUntieredTariffDeclaration(
        scope=UsageOperationScope(resource=resource),
        billing_basis=(
            "aggregate_input_tokens"
            if resource.endswith("embeddings.create")
            else "aggregate_input_output_tokens"
        ),
    )
    model = {
        "model_id": "untiered-contract",
        "input_cost_per_mtok": 2,
        "output_cost_per_mtok": 4,
        "untiered_tariff": declaration.model_dump(mode="json"),
    }
    return ModelInfo(
        **model,
        provider_metadata={
            "field_sources": {
                "input_cost_per_mtok": "configured",
                "output_cost_per_mtok": "configured",
                "untiered_tariff": "configured",
            }
        },
    ), {"models": [model]}


def proof(resource: str = "openai.embeddings.create") -> UsageServiceTierApplicability:
    return UsageServiceTierApplicability(
        state="not_applicable", scope=UsageOperationScope(resource=resource)
    )


def selection(evidence: UsageServiceTierApplicability | None) -> UsageModelSelection:
    return UsageModelSelection(
        provider_id="untiered-contract",
        effective_model="untiered-contract",
        operation="embeddings",
        source="adapter",
        streaming=False,
        service_tier_applicability=evidence,
    )


def test_applicability_has_three_states_and_an_exact_versioned_scope() -> None:
    for state in ("unknown", "applicable"):
        UsageServiceTierApplicability(state=state)
        with pytest.raises(ValidationError):
            UsageServiceTierApplicability(state=state, scope=proof().scope)
    with pytest.raises(ValidationError):
        UsageServiceTierApplicability(state="not_applicable")
    for field in ("version", "abi_revision"):
        with pytest.raises(ValidationError):
            UsageOperationScope.model_validate(
                {
                    **proof().scope.model_dump(),
                    field: 2,
                }
            )
    with pytest.raises(ValidationError):
        UsageUntieredTariffDeclaration(
            scope=proof().scope, billing_basis="aggregate_input_output_tokens"
        )


def test_new_fingerprint_domain_does_not_change_legacy_identity() -> None:
    info, config = tariff()
    legacy_info = info.model_copy(update={"untiered_tariff": None})
    legacy_config = {
        "models": [
            {key: value for key, value in config["models"][0].items() if key != "untiered_tariff"}
        ]
    }
    args = {"provider_id": "untiered-contract", "model": "untiered-contract"}
    old = usage_pricing_fingerprint(**args, model_info=legacy_info, provider_config=legacy_config)
    assert old == usage_pricing_fingerprint(**args, model_info=info, provider_config=config)
    new = usage_pricing_fingerprint(
        **args, model_info=info, provider_config=config, applicability=proof()
    )
    assert new != old
    assert new != usage_pricing_fingerprint(
        **args,
        model_info=info,
        provider_config=config,
        applicability=proof("openai.images.generate"),
    )


@pytest.mark.parametrize("missing", ["declaration", "configured-rate", "provenance", "rates"])
def test_scalar_prices_and_defaults_cannot_mint_a_tariff(missing: str) -> None:
    info, config = tariff()
    if missing == "declaration":
        config["models"][0].pop("untiered_tariff")
    elif missing == "configured-rate":
        config["models"][0].pop("input_cost_per_mtok")
    elif missing == "provenance":
        info.provider_metadata["field_sources"]["untiered_tariff"] = "catalog:litellm"
    else:
        info = info.model_copy(update={"input_cost_per_mtok": None})
    _, price = metadata_price(
        provider_id="untiered-contract",
        model="untiered-contract",
        info=info,
        config=config,
        now=datetime.now(UTC),
    )
    assert price is None


@pytest.mark.parametrize(
    "mutation", ["legacy", "unknown", "wrong-scope", "wrong-operation", "concrete-tier"]
)
def test_valuation_requires_independent_matching_scope(mutation: str) -> None:
    info, config = tariff()
    _, price = metadata_price(
        provider_id="untiered-contract",
        model="untiered-contract",
        info=info,
        config=config,
        now=datetime.now(UTC),
    )
    accumulator = UsageAccumulator("openai")
    accumulator.observe({"usage": {"prompt_tokens": 11, "total_tokens": 11}})
    usage = accumulator.normalized(partial=False)
    chosen = selection(proof())
    assert calculate_price(usage, chosen, price).cost == Decimal("0.000022")
    if mutation == "legacy":
        chosen = selection(None)
    elif mutation == "unknown":
        chosen = selection(UsageServiceTierApplicability(state="unknown"))
    elif mutation == "wrong-scope":
        chosen = selection(proof("openai.images.generate"))
    elif mutation == "wrong-operation":
        chosen = chosen.model_copy(update={"operation": "native"})
    else:
        chosen = chosen.model_copy(update={"service_tier": "priority"})
    assert calculate_price(usage, chosen, price).cost is None


def test_old_null_tariffs_do_not_acquire_not_applicable_meaning() -> None:
    info, config = tariff()
    _, price = metadata_price(
        provider_id="untiered-contract",
        model="untiered-contract",
        info=info,
        config=config,
        now=datetime.now(UTC),
    )
    assert price is not None
    legacy = type(price).model_validate(
        {
            **price.model_dump(exclude={"untiered_tariff"}),
            "rule_version": 1,
        }
    )
    accumulator = UsageAccumulator("openai")
    accumulator.observe({"usage": {"prompt_tokens": 11, "total_tokens": 11}})
    assert (
        calculate_price(
            accumulator.normalized(partial=False),
            selection(proof()),
            legacy,
        ).cost
        is None
    )


@pytest.mark.parametrize(
    "counts", [{}, {"input_tokens": 7}, {"input_tokens": 7, "output_tokens": 0}]
)
def test_image_eligibility_does_not_supply_missing_native_units(counts: dict[str, int]) -> None:
    info, config = tariff("openai.images.generate")
    _, price = metadata_price(
        provider_id="untiered-contract",
        model="untiered-contract",
        info=info,
        config=config,
        now=datetime.now(UTC),
    )
    chosen = selection(proof("openai.images.generate")).model_copy(update={"operation": "native"})
    accumulator = UsageAccumulator("openai")
    accumulator.observe({"usage": counts})
    result = calculate_price(accumulator.normalized(partial=False), chosen, price)
    assert (result.status == "complete") == ("output_tokens" in counts)
    if not counts:
        assert result.cost is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_response", [False, True])
@pytest.mark.parametrize(
    "override",
    [
        {"extra_body": {"service_tier": "priority"}},
        {"extra_body": {"service_tier": None}},
        {"extra_query": {"service_tier": "auto"}},
        {"extra_headers": {"x-tier": "priority"}},
    ],
)
async def test_installed_resource_overrides_capture_but_cannot_mint_proof(
    override: dict[str, Any],
    raw_response: bool,
) -> None:
    sink = Sink()
    req = request(sink)
    client = AsyncOpenAI(
        api_key="local-test-transport-only",
        base_url="https://usage-untiered.test/v1",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda wire: httpx.Response(
                    200,
                    request=wire,
                    json={
                        "model": "untiered-contract",
                        "object": "list",
                        "data": [{"object": "embedding", "index": 0, "embedding": [0.5]}],
                        "usage": {"prompt_tokens": 11, "total_tokens": 11},
                    },
                )
            )
        ),
        max_retries=0,
    )
    binding = UsageBinding(req, "provider-test", "untiered-contract")
    try:
        function = (
            client.embeddings.with_raw_response.create if raw_response else client.embeddings.create
        )
        await observed_sdk(function, binding, operation="embeddings")(
            model="untiered-contract", input=["input"], **override
        )
        finals = [e for e in sink.offers if isinstance(e, UsageAttemptFinalized)]
        assert len(finals) == 1
        assert finals[0].context.selection.service_tier_applicability is None
        assert finals[0].context.selection.captured_price_revision_id is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "mutation",
    ["none", "missing", "abi", "scope", "operation", "provider", "model", "correlation"],
)
def test_executor_candidate_is_frozen_scoped_and_never_chosen_from_returned_ids(
    mutation: str,
) -> None:
    """Protocol negatives; real physical qualification is in the boundary matrix."""
    info, config = tariff("openai.images.generate")
    fingerprint, definition = metadata_price(
        provider_id="untiered-contract",
        model="untiered-contract",
        info=info,
        config=config,
        now=datetime.now(UTC),
    )
    assert definition is not None
    cache = UsagePricePinCache()
    cache.replace(
        {
            ("untiered-contract", "untiered-contract", fingerprint): UsagePricingPin(
                definition.revision_id,
                fingerprint,
            )
        }
    )
    sink = Sink()
    capture = UsageCapture(
        sink=sink,
        controller_owner_id="controller-test",
        coverage_epoch_id="coverage-test",
        price_lookup=cache,
    )
    req = capture.request(
        attribution=request(Sink()).attribution,
        requested_model="untiered-contract",
    )
    binding = UsageBinding(
        req,
        "untiered-contract",
        "untiered-contract",
        model_info=info,
        provider_config=config,
    )
    metadata, seed = executor_metadata(
        binding,
        None,
        executor_id="untiered-executor",
        parameters={},
        streaming=False,
    )
    assert seed is not None
    observer = executor_observer(metadata)
    assert observer is not None
    attempt = observer.binding.begin(
        operation="native",
        streaming=False,
        parameters={},
        applicability=proof("openai.images.generate"),
        semantic_profile="openai",
    )
    assert attempt is not None
    attempt.observe({"usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}})
    attempt.finish()
    result = observer.attach({})
    # A cold or changed live cache must not affect the dispatch-owned candidate.
    cache.invalidate()
    for raw in result["backend_metadata"]["cognis_usage_observations"]:
        chosen = raw["context"]["selection"]
        chosen["captured_price_revision_id"] = "upr_executor_untrusted_claim"
        chosen["resolver_snapshot_id"] = "upm_executor_untrusted_claim"
        if mutation == "missing":
            chosen["service_tier_applicability"] = None
        elif mutation == "abi":
            chosen["service_tier_applicability"]["scope"]["abi_revision"] = 2
        elif mutation == "scope":
            chosen["service_tier_applicability"]["scope"]["resource"] = "openai.embeddings.create"
        elif mutation == "operation":
            chosen["operation"] = "embeddings"
        elif mutation == "provider":
            chosen["provider_id"] = "different-provider"
        elif mutation == "model":
            chosen["effective_model"] = "different-model"
        elif mutation == "correlation":
            raw["context"]["executor"]["inference_request_id"] = "different-inference"
    ingest_executor(result, binding, seed)
    finals = [item for item in sink.offers if isinstance(item, UsageAttemptFinalized)]
    if mutation in {"provider", "model", "correlation"}:
        assert not finals
        assert capture.failed_events > 0
    else:
        assert len(finals) == 1
        chosen = finals[0].context.selection
        assert finals[0].usage.input_tokens.value == 7
        if mutation == "none":
            assert chosen.captured_price_revision_id == definition.revision_id
            assert chosen.resolver_snapshot_id == fingerprint
        else:
            assert chosen.service_tier_applicability is None
            assert chosen.captured_price_revision_id is None
            assert chosen.resolver_snapshot_id is None


def test_executor_candidate_and_configuration_budgets_remain_bounded() -> None:
    info, config = tariff("openai.images.generate")
    evidence = proof("openai.images.generate")
    assert (
        configured_untiered_model_info({"models": config["models"] * 257}, "untiered-contract")
        is None
    )
    fingerprint = usage_pricing_fingerprint(
        provider_id="untiered-contract",
        model="untiered-contract",
        model_info=info,
        provider_config=config,
        applicability=evidence,
    )
    lookup_calls = []

    def lookup(**kwargs: Any) -> UsagePricingPin:
        lookup_calls.append(kwargs)
        return UsagePricingPin("upr_bounded_candidate", fingerprint)

    capture = UsageCapture(
        sink=Sink(),
        controller_owner_id="controller-test",
        coverage_epoch_id="coverage-test",
        price_lookup=lookup,
    )
    req = capture.request(attribution=request(Sink()).attribution)
    binding = UsageBinding(
        req,
        "untiered-contract",
        "untiered-contract",
        model_info=info,
        provider_config=config,
    )
    for _ in range(40):
        executor_metadata(
            binding,
            None,
            executor_id="untiered-executor",
            parameters={},
            streaming=False,
        )
    assert len(req.executor_price_candidates) == len(lookup_calls) == 32
