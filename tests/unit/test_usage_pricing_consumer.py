"""Price pins come only from exact committed snapshots, never inference-side I/O."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from cognis.models.config import ModelInfo
from cognis.models.usage import UsageAttribution, UsageIdentity
from cognis.models.usage_pricing import (
    UsagePricingLookup,
    UsagePricingPin,
    usage_pricing_fingerprint,
)
from cognis.providers.llm.usage_binding import UsageBinding, take_binding
from cognis.providers.llm.usage_capture import UsageAttempt, UsageCapture
from tests.unit.test_usage_capture_runtime import Sink

PIN_A = UsagePricingPin("upr_revision_a", "ups_snapshot_a")
PIN_B = UsagePricingPin("upr_revision_b", "ups_snapshot_b")
INFO = ModelInfo(model_id="actual-model", tier="priority", input_cost_per_mtok=2)
CONFIG = {"preset": "openai", "api_key": "PRIVATE CREDENTIAL"}
PARAMETERS = {"service_tier": "priority"}


class Snapshot:
    """Fixture: storage's atomically replaced, exact-key memory view."""

    def __init__(self) -> None:
        self.pins: dict[tuple[str, str, str], UsagePricingPin] = {}
        self.calls: list[tuple[str, str, str]] = []

    def publish(
        self,
        pin: UsagePricingPin = PIN_A,
        *,
        provider: str = "provider-a",
        model: str = "actual-model",
        info: ModelInfo = INFO,
        config: dict[str, Any] = CONFIG,
        tier: str = "priority",
    ) -> None:
        fingerprint = usage_pricing_fingerprint(
            provider_id=provider,
            model=model,
            model_info=info,
            provider_config=config,
            service_tier=tier,
        )
        self.pins = {**self.pins, (provider, model, fingerprint): pin}

    def __call__(
        self, *, provider_id: str, model: str, metadata_fingerprint: str
    ) -> UsagePricingPin | None:
        key = (provider_id, model, metadata_fingerprint)
        self.calls.append(key)
        return self.pins.get(key)


def binding(
    lookup: UsagePricingLookup | None,
    *,
    provider: str = "provider-a",
    model: str = "actual-model",
    info: ModelInfo | None = INFO,
    config: dict[str, Any] | None = CONFIG,
) -> tuple[Sink, UsageBinding]:
    sink = Sink()
    capture = UsageCapture(
        sink=sink,
        controller_owner_id="controller-pricing",
        coverage_epoch_id="coverage-pricing",
        price_lookup=lookup,
    )
    request = capture.request(
        attribution=UsageAttribution(
            identity=UsageIdentity(
                principal_type="user",
                principal_subject="actor@test",
                usage_subject_kind="user",
                usage_subject_id="actor@test",
            ),
            purpose="pricing_test",
        ),
        requested_model="routes/interactive",
    )
    return sink, UsageBinding(request, provider, model, model_info=info, provider_config=config)


def start(target: UsageBinding, parameters: dict[str, Any] = PARAMETERS) -> UsageAttempt:
    attempt = target.begin(operation="chat", streaming=False, parameters=parameters)
    assert attempt is not None
    return attempt


def refs(attempt: UsageAttempt) -> tuple[str | None, str | None]:
    selection = attempt.context.selection
    return selection.captured_price_revision_id, selection.resolver_snapshot_id


def test_constructor_keyword_only_dependency_and_exact_hit() -> None:
    parameter = inspect.signature(UsageCapture).parameters["price_lookup"]
    assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    snapshot = Snapshot()
    snapshot.publish()
    sink, target = binding(snapshot)
    assert target.request.capture.price_lookup is snapshot
    attempt = start(target)
    assert refs(attempt) == (PIN_A.captured_price_revision_id, PIN_A.resolver_snapshot_id)
    assert attempt.context.selection.effective_model == "actual-model"
    assert attempt.context.selection.requested_model == "routes/interactive"
    assert snapshot.calls == list(snapshot.pins)
    attempt.finish()
    assert sink.offers[0].context == sink.offers[1].context
    assert "PRIVATE CREDENTIAL" not in sink.offers[0].model_dump_json()


@pytest.mark.parametrize("change", ["provider", "model", "metadata", "configuration"])
def test_exact_miss_never_falls_back_to_a_model_only_price(change: str) -> None:
    snapshot = Snapshot()
    snapshot.publish()
    kwargs: dict[str, Any] = {
        "provider": "provider-b" if change == "provider" else "provider-a",
        "model": "other-model" if change == "model" else "actual-model",
        "info": INFO.model_copy(update={"input_cost_per_mtok": 3})
        if change == "metadata"
        else INFO,
        "config": {**CONFIG, "preset": "openai_compatible"}
        if change == "configuration"
        else CONFIG,
    }
    _, target = binding(snapshot, **kwargs)
    attempt = start(target)
    assert refs(attempt) == (None, None)
    assert len(snapshot.calls) == 1
    assert snapshot.calls[0] not in snapshot.pins
    attempt.finish()


def test_provider_model_fallback_uses_the_actual_target_and_its_own_pin() -> None:
    snapshot = Snapshot()
    snapshot.publish()
    other = INFO.model_copy(update={"model_id": "fallback-model", "input_cost_per_mtok": 4})
    snapshot.publish(PIN_B, provider="provider-b", model="fallback-model", info=other)
    sink, first = binding(snapshot)
    before = start(first)
    before.finish()
    fallback = UsageBinding(
        first.request, "provider-b", "fallback-model", model_info=other, provider_config=CONFIG
    )
    after = start(fallback)
    after.finish()
    assert refs(before) == (PIN_A.captured_price_revision_id, PIN_A.resolver_snapshot_id)
    assert refs(after) == (PIN_B.captured_price_revision_id, PIN_B.resolver_snapshot_id)
    assert after.context.trace.fallback_from_attempt_id == before.context.trace.attempt_id
    assert [call[:2] for call in snapshot.calls] == [
        ("provider-a", "actual-model"),
        ("provider-b", "fallback-model"),
    ]
    assert len(sink.offers) == 4


@pytest.mark.parametrize("tier", [None, "", "auto", "AUTO", "unknown", " ", False, 1])
def test_unknown_or_auto_actual_tier_never_silently_pins_standard(tier: Any) -> None:
    calls: list[dict[str, Any]] = []

    def permissive_lookup(**kwargs: Any) -> UsagePricingPin:
        calls.append(kwargs)
        return PIN_A

    _, target = binding(permissive_lookup, info=INFO.model_copy(update={"tier": "standard"}))
    attempt = start(target, {"service_tier": tier})
    assert refs(attempt) == (None, None)
    assert calls == []
    attempt.finish()


def test_concrete_tier_must_match_and_omitted_tier_uses_model_default() -> None:
    snapshot = Snapshot()
    snapshot.publish()
    _, target = binding(snapshot)
    priority = start(target)
    priority.finish()
    changed = start(target, {"service_tier": "flex"})
    changed.finish()
    omitted = start(target, {})
    omitted.finish()
    unknown = start(target, {"service_tier": "unrecognized"})
    unknown.finish()
    pinned = (PIN_A.captured_price_revision_id, PIN_A.resolver_snapshot_id)
    assert refs(priority) == pinned
    assert refs(changed) == refs(unknown) == (None, None)
    # The fixture model's default tier is "priority", so an omitted tier bills there.
    assert refs(omitted) == pinned
    assert len(snapshot.calls) == 3
    assert snapshot.calls[0][2] == snapshot.calls[2][2] != snapshot.calls[1][2]


@pytest.mark.parametrize("missing", ["lookup", "metadata", "configuration"])
def test_missing_dependency_or_resolved_metadata_is_unpriced(missing: str) -> None:
    snapshot = Snapshot()
    snapshot.publish()
    _, target = binding(
        None if missing == "lookup" else snapshot,
        info=None if missing == "metadata" else INFO,
        config=None if missing == "configuration" else CONFIG,
    )
    attempt = start(target)
    assert refs(attempt) == (None, None)
    assert snapshot.calls == []
    attempt.finish()


@pytest.mark.parametrize("failure", ["lookup", "fingerprint", "invalid_pin", "async_lookup"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_pricing_failure_preserves_dispatch_result_exception_and_cancellation(
    failure: str, outcome: str
) -> None:
    snapshot = Snapshot()
    calls = 0
    response = {"usage": {"prompt_tokens": 2}}
    original = asyncio.CancelledError() if outcome == "cancelled" else RuntimeError("upstream")

    def broken(**kwargs: Any) -> UsagePricingPin | None:
        if failure == "invalid_pin":
            return UsagePricingPin("", "ups_valid_snapshot")
        raise RuntimeError("PRIVATE LOOKUP FAILURE")

    async def asynchronous(**kwargs: Any) -> UsagePricingPin | None:
        raise AssertionError("Must not await lookup")

    lookup = cast(UsagePricingLookup, asynchronous) if failure == "async_lookup" else broken
    if failure == "fingerprint":
        lookup = snapshot
    info = (
        INFO.model_copy(update={"pricing": {"rate": float("nan")}})
        if failure == "fingerprint"
        else INFO
    )
    sink, target = binding(lookup, info=info)

    async def dispatch() -> dict[str, dict[str, int]]:
        nonlocal calls
        calls += 1
        assert len(sink.offers) == 1
        if outcome != "success":
            raise original
        return response

    if outcome == "success":
        assert (
            await target.call(
                dispatch,
                operation="chat",
                parameters=PARAMETERS,
            )
            is response
        )
    else:
        with pytest.raises(type(original)) as caught:
            await target.call(dispatch, operation="chat", parameters=PARAMETERS)
        assert caught.value is original
    assert calls == 1
    assert target.request.capture.failed_events == 1
    assert len(sink.offers) == 2
    assert sink.offers[0].context == sink.offers[1].context
    assert sink.offers[-1].context.selection.resolver_snapshot_id is None
    assert sink.offers[-1].context.selection.captured_price_revision_id is None
    assert "PRIVATE LOOKUP FAILURE" not in sink.offers[-1].model_dump_json()
    assert snapshot.calls == []


def test_started_and_finalized_refs_are_frozen_across_snapshot_replacement() -> None:
    snapshot = Snapshot()
    snapshot.publish()
    sink, target = binding(snapshot)
    attempt = start(target)
    snapshot.publish(PIN_B)
    attempt.finish()
    assert len(snapshot.calls) == 1
    for event in sink.offers:
        assert (
            event.context.selection.captured_price_revision_id == PIN_A.captured_price_revision_id
        )
        assert event.context.selection.resolver_snapshot_id == PIN_A.resolver_snapshot_id
    next_attempt = start(target)
    assert refs(next_attempt) == (PIN_B.captured_price_revision_id, PIN_B.resolver_snapshot_id)
    next_attempt.finish()


def test_committed_snapshot_with_unknown_price_does_not_invent_zero() -> None:
    snapshot = Snapshot()
    snapshot.publish(UsagePricingPin(None, PIN_A.resolver_snapshot_id))
    _, target = binding(snapshot)
    attempt = start(target)
    assert refs(attempt) == (None, PIN_A.resolver_snapshot_id)
    attempt.finish()


async def test_resolved_controller_binding_receives_metadata_without_additional_lookup_io() -> None:
    from cognis.providers.llm.litellm import LiteLLMProvider
    from cognis.store.models import LLMProvider

    snapshot = Snapshot()
    snapshot.publish()
    _, target = binding(snapshot)
    provider = LiteLLMProvider(MagicMock(), usage_capture=target.request.capture)
    provider._resolve_provider_kwargs = AsyncMock(return_value=PARAMETERS)
    provider.get_model_info = AsyncMock(side_effect=AssertionError("No extra metadata I/O"))
    row = LLMProvider(provider_id="provider-a", owner_email="payer@test", config=CONFIG)
    kwargs = await provider._inference_kwargs(
        row,
        INFO.model_id,
        "chat",
        {},
        actor="actor@test",
        model_info=INFO,
    )
    resolved = take_binding(kwargs)
    assert resolved is not None
    attempt = start(resolved, kwargs)
    assert refs(attempt) == (PIN_A.captured_price_revision_id, PIN_A.resolver_snapshot_id)
    assert attempt.context.attribution.identity.provider_payer == "payer@test"
    provider.get_model_info.assert_not_awaited()
    assert not any(key.startswith("_cognis_") for key in kwargs)
    attempt.finish()
