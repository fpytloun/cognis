"""Recovery ownership, stream commitment and retry counts."""

import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from cognis.providers.llm.availability import InferenceAvailability
from cognis.providers.llm.errors import (
    LLMStreamProviderError,
    ProviderReauthorizationRequired,
    classify_llm_exception,
)
from cognis.providers.llm.recovery import (
    InferenceCandidate,
    InferencePlan,
    InferenceRecovery,
    commits_output,
)
from cognis.providers.llm.recovery_policy import RecoveryPolicy, RouteTarget


def plan(*, retries=1, targets=2):
    return InferencePlan(
        reference="cognis/chat",
        owner="user@cognis.local",
        candidates=tuple(
            InferenceCandidate(
                RouteTarget(provider_id=f"provider-{i}", model="chat", priority=i),
                f"fingerprint-{i}",
            )
            for i in range(targets)
        ),
        policy=RecoveryPolicy(max_retries_per_target=retries),
    )


async def collect(stream):
    return [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_unclassified_failure_recovers_before_output(recovery_clock):
    now, sleeps = recovery_clock
    calls = []

    async def attempt(candidate, budget):
        calls.append(budget.attempts)
        if len(calls) == 1:
            raise RuntimeError("Temporary inference admission fault")
        yield {"choices": [{"delta": {"content": "recovered"}}]}

    chunks = await collect(
        InferenceRecovery(InferenceAvailability()).stream(
            plan(targets=1), attempt, deadline=now[0] + 60
        )
    )
    assert calls == [1, 2]
    assert len(sleeps) == 1
    assert chunks[-1]["choices"][0]["delta"]["content"] == "recovered"


@pytest.mark.asyncio
async def test_exhaustion_is_explicit_but_admission_failure_is_not(recovery_clock):
    from cognis.core.redis_service import RedisUnavailableError

    now, _ = recovery_clock

    async def attempt(candidate, budget):
        raise LLMStreamProviderError("Connection reset", payload={"category": "connection"})
        yield

    with pytest.raises(LLMStreamProviderError) as exhausted:
        await collect(
            InferenceRecovery(InferenceAvailability()).stream(
                plan(targets=1, retries=0), attempt, deadline=now[0] + 60
            )
        )
    assert classify_llm_exception(exhausted.value)["recovery_exhausted"] is True

    availability = InferenceAvailability()
    availability.limit = AsyncMock(side_effect=RedisUnavailableError("Admission state unavailable"))
    with pytest.raises(RedisUnavailableError) as admission:
        await collect(
            InferenceRecovery(availability).stream(plan(targets=1), attempt, deadline=now[0] + 60)
        )
    assert classify_llm_exception(admission.value).get("recovery_exhausted") is not True


@pytest.fixture
def recovery_clock(monkeypatch):
    now = [time.monotonic()]
    sleeps = []
    monkeypatch.setattr("cognis.providers.llm.recovery.time.monotonic", lambda: now[0])

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr("cognis.providers.llm.recovery.asyncio.sleep", sleep)
    return now, sleeps


@pytest.mark.asyncio
async def test_preblocked_limits_propagate_rate_limit_without_transport(recovery_clock):
    now, sleeps = recovery_clock
    availability = InferenceAvailability()
    await availability.limit("fingerprint-0", block_seconds=300)
    await availability.limit("fingerprint-1", block_seconds=120)
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate)
        yield

    with pytest.raises(LLMStreamProviderError) as failure:
        await collect(InferenceRecovery(availability).stream(plan(), attempt, deadline=now[0] + 60))
    payload = classify_llm_exception(failure.value)
    assert payload["category"] == "rate_limit"
    assert payload["retry_after_seconds"] == 120
    assert calls == sleeps == []


@pytest.mark.asyncio
async def test_preblocked_waits_for_earliest_target_not_highest_priority(recovery_clock):
    now, sleeps = recovery_clock
    availability = InferenceAvailability()
    await availability.limit("fingerprint-0", block_seconds=300)
    await availability.limit("fingerprint-1", block_seconds=12)
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        yield {"choices": [{"delta": {"content": "recovered"}}]}

    await collect(InferenceRecovery(availability).stream(plan(), attempt, deadline=now[0] + 60))
    assert sleeps == [12]
    assert calls == ["provider-1"]


@pytest.mark.asyncio
async def test_deferred_target_beats_an_unvisited_preblocked_target(recovery_clock):
    now, sleeps = recovery_clock
    availability = InferenceAvailability()
    await availability.limit("fingerprint-1", block_seconds=300)
    request_plan = replace(
        plan(),
        policy=RecoveryPolicy(
            max_retries_per_target=1, target_recovery_window_seconds=1, cooldown_seconds=1
        ),
    )
    calls = []

    async def attempt(candidate, budget):
        calls.append((candidate.target.provider_id, budget.attempts))
        if len(calls) == 1:
            raise LLMStreamProviderError(
                "busy", payload={"category": "rate_limit", "retry_after_seconds": 5}
            )
        yield {"choices": [{"delta": {"content": "recovered"}}]}

    await collect(
        InferenceRecovery(availability).stream(request_plan, attempt, deadline=now[0] + 60)
    )
    assert calls == [("provider-0", 1), ("provider-0", 2)]
    assert sleeps == [5]


@pytest.mark.asyncio
async def test_terminal_retry_after_includes_immediately_admissible_targets(recovery_clock):
    now, _ = recovery_clock

    async def attempt(candidate, budget):
        payload = {"category": "connection"}
        if candidate.target.provider_id == "provider-1":
            payload = {"category": "rate_limit", "retry_after_seconds": 120}
        raise LLMStreamProviderError("upstream failure", payload=payload)
        yield

    with pytest.raises(LLMStreamProviderError) as failure:
        await collect(
            InferenceRecovery(InferenceAvailability()).stream(
                plan(retries=0), attempt, deadline=now[0] + 60
            )
        )
    assert failure.value.to_payload()["category"] == "rate_limit"
    assert failure.value.to_payload()["retry_after_seconds"] == 0


@pytest.mark.asyncio
async def test_both_new_limits_retry_soonest_without_resetting_attempt_budget(recovery_clock):
    now, sleeps = recovery_clock
    availability = InferenceAvailability()
    request_plan = plan()
    request_plan = replace(
        request_plan,
        policy=RecoveryPolicy(
            max_retries_per_target=1,
            target_recovery_window_seconds=1,
            cooldown_seconds=1,
        ),
    )
    calls = []

    async def attempt(candidate, budget):
        calls.append((candidate.target.provider_id, budget.attempts))
        if len(calls) <= 2:
            raise LLMStreamProviderError(
                "busy",
                payload={
                    "category": "rate_limit",
                    "retry_after_seconds": 20
                    if candidate.target.provider_id == "provider-0"
                    else 5,
                },
            )
        yield {"choices": [{"delta": {"content": "recovered"}}]}

    await collect(
        InferenceRecovery(availability).stream(request_plan, attempt, deadline=now[0] + 60)
    )
    assert calls == [("provider-0", 1), ("provider-1", 1), ("provider-1", 2)]
    assert sleeps == [5]


@pytest.mark.asyncio
async def test_all_attempted_limits_expose_route_earliest_retry(recovery_clock):
    now, _ = recovery_clock

    async def attempt(candidate, budget):
        raise LLMStreamProviderError(
            "upstream busy",
            payload={
                "category": "rate_limit",
                "code": "rate_limit_error",
                "retry_after_seconds": 120 if candidate.target.provider_id == "provider-0" else 300,
            },
        )
        yield

    with pytest.raises(LLMStreamProviderError) as failure:
        await collect(
            InferenceRecovery(InferenceAvailability()).stream(
                plan(retries=0), attempt, deadline=now[0] + 60
            )
        )
    assert failure.value.to_payload() == {
        "category": "rate_limit",
        "code": "rate_limit_error",
        "retry_after_seconds": 120,
        "recovery_exhausted": True,
    }


@pytest.mark.asyncio
async def test_shared_circuit_retains_non_rate_limit_failure_category(recovery_clock):
    now, _ = recovery_clock
    availability = InferenceAvailability()
    request_plan = plan()
    for candidate in request_plan.candidates:
        key = availability.key(
            target_fingerprint=candidate.fingerprint,
            policy=request_plan.policy,
            scope=request_plan.policy_scope,
        )
        permit = await availability.admit(key, request_plan.policy, lease_seconds=10)
        await availability.failed(
            permit,
            request_plan.policy,
            request_id=candidate.fingerprint,
            retry_after_seconds=120,
            failure_category="provider_5xx",
        )

    async def attempt(candidate, budget):
        pytest.fail("Blocked upstream must not be called")
        yield

    with pytest.raises(LLMStreamProviderError) as failure:
        await collect(
            InferenceRecovery(availability).stream(request_plan, attempt, deadline=now[0] + 60)
        )
    assert classify_llm_exception(failure.value)["category"] == "provider_5xx"
    assert failure.value.to_payload()["retry_after_seconds"] == 120


@pytest.mark.asyncio
async def test_known_reset_wait_is_cancellable_without_dispatch():
    import asyncio
    from unittest.mock import AsyncMock

    availability = InferenceAvailability()
    await availability.limit("fingerprint-0", block_seconds=60)
    attempt = AsyncMock()
    task = asyncio.create_task(
        collect(
            InferenceRecovery(availability).stream(
                plan(targets=1), attempt, deadline=time.monotonic() + 120
            )
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempt.assert_not_called()


@pytest.mark.asyncio
async def test_incompatible_fallback_preserves_original_rate_limit():
    from cognis.providers.llm.recovery import IncompatibleCandidate

    async def prepare(candidate, budget):
        if candidate.target.provider_id == "provider-1":
            raise IncompatibleCandidate("History requires its native provider")

    async def attempt(candidate, budget, prepared):
        raise LLMStreamProviderError(
            "rate limited",
            payload={
                "category": "rate_limit",
                "retry_after_seconds": 3600,
            },
        )
        yield

    with pytest.raises(LLMStreamProviderError) as failure:
        await collect(
            InferenceRecovery(InferenceAvailability()).stream(
                plan(retries=0), attempt, prepare=prepare, deadline=time.monotonic() + 10
            )
        )
    assert failure.value.to_payload()["category"] == "rate_limit"
    assert failure.value.to_payload()["retry_after_seconds"] == 3600


@pytest.mark.asyncio
async def test_retries_then_fallback_discard_preludes(monkeypatch):
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 0)
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        if candidate.target.provider_id == "provider-0":
            raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})
        yield {"choices": [{"delta": {"content": "answer"}}]}

    result = await collect(
        InferenceRecovery(InferenceAvailability()).stream(
            plan(), attempt, deadline=time.monotonic() + 10
        )
    )
    assert calls == ["provider-0", "provider-0", "provider-1"]
    assert len(result) == 2


@pytest.mark.asyncio
async def test_committed_stream_never_switches():
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})

    with pytest.raises(LLMStreamProviderError):
        await collect(
            InferenceRecovery(InferenceAvailability()).stream(
                plan(), attempt, deadline=time.monotonic() + 10
            )
        )
    assert calls == ["provider-0"]


@pytest.mark.asyncio
async def test_non_retryable_error_never_falls_back():
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        raise LLMStreamProviderError("bad input", payload={"category": "invalid_request"})
        yield

    with pytest.raises(LLMStreamProviderError):
        await collect(
            InferenceRecovery(InferenceAvailability()).stream(
                plan(), attempt, deadline=time.monotonic() + 10
            )
        )
    assert calls == ["provider-0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", [1, 2])
async def test_rejected_credentials_skip_same_target_retries(recovery_clock, targets):
    now, sleeps = recovery_clock
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        if candidate.target.provider_id == "provider-0":
            raise ProviderReauthorizationRequired(provider_id="provider-0", provider_label="Claude")
        yield {"choices": [{"delta": {"content": "fallback"}}]}

    stream = InferenceRecovery(InferenceAvailability()).stream(
        plan(targets=targets, retries=3), attempt, deadline=now[0] + 60
    )
    if targets == 2:
        chunks = await collect(stream)
        assert chunks[-1]["choices"][0]["delta"]["content"] == "fallback"
        assert calls == ["provider-0", "provider-1"]
    else:
        with pytest.raises(LLMStreamProviderError) as failure:
            await collect(stream)
        payload = classify_llm_exception(failure.value)
        assert payload["category"] == "authentication"
        assert payload["code"] == "reauthorization_required"
        assert payload["message"].startswith("Claude sign-in expired or was revoked.")
        assert calls == ["provider-0"]
    assert sleeps == []


@pytest.mark.asyncio
async def test_long_retry_after_switches_without_sleep():
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        if len(calls) == 1:
            yield {"response_error": {"category": "rate_limit", "retry_after_seconds": 60}}
        else:
            yield {"choices": [{"delta": {"content": "answer"}}]}

    await collect(
        InferenceRecovery(InferenceAvailability()).stream(
            plan(), attempt, deadline=time.monotonic() + 10
        )
    )
    assert calls == ["provider-0", "provider-1"]


@pytest.mark.asyncio
async def test_empty_success_does_not_trigger_retry():
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    result = await collect(
        InferenceRecovery(InferenceAvailability()).stream(
            plan(), attempt, deadline=time.monotonic() + 10
        )
    )
    assert len(calls) == 1 and len(result) == 1


@pytest.mark.parametrize(
    "delta",
    [
        {"refusal": "refused"},
        {"tool_calls": [{"id": "tool"}]},
        {"reasoning_content": "thinking"},
        {"content": "hello"},
    ],
)
def test_semantic_commit(delta):
    assert commits_output({"choices": [{"delta": delta}]})
    assert not commits_output({"choices": [{"delta": {"role": "assistant"}}]})


@pytest.mark.asyncio
async def test_failed_committed_half_open_trial_reopens_circuit(monkeypatch):
    now = [time.monotonic()]
    monkeypatch.setattr("cognis.providers.llm.availability.time.monotonic", lambda: now[0])
    request_plan = plan(targets=1)
    policy = request_plan.policy
    availability = InferenceAvailability()
    key = availability.key(
        target_fingerprint="fingerprint-0", policy=policy, scope=request_plan.policy_scope
    )
    first = await availability.admit(key, policy, lease_seconds=10)
    await availability.failed(first, policy, request_id="opening", retry_after_seconds=30)
    now[0] += 31

    async def attempt(candidate, budget):
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})

    with pytest.raises(LLMStreamProviderError):
        await collect(
            InferenceRecovery(availability).stream(request_plan, attempt, deadline=now[0] + 10)
        )
    assert not (await availability.admit(key, policy, lease_seconds=10)).allowed


@pytest.mark.asyncio
async def test_backoff_oversleep_exhausts_target_not_request(monkeypatch):
    now = [time.monotonic()]
    monkeypatch.setattr("cognis.providers.llm.recovery.time.monotonic", lambda: now[0])
    monkeypatch.setattr("cognis.providers.llm.recovery.compute_delay", lambda *args: 1)

    async def delayed_wakeup(delay):
        now[0] += 31

    monkeypatch.setattr("cognis.providers.llm.recovery.asyncio.sleep", delayed_wakeup)
    calls = []

    async def attempt(candidate, budget):
        calls.append(candidate.target.provider_id)
        if candidate.target.provider_id == "provider-0":
            raise LLMStreamProviderError("unavailable", payload={"category": "provider_5xx"})
        yield {"choices": [{"delta": {"content": "answer"}}]}

    await collect(
        InferenceRecovery(InferenceAvailability()).stream(plan(), attempt, deadline=now[0] + 100)
    )
    assert calls == ["provider-0", "provider-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [True, False])
async def test_prelude_limit_does_not_limit_semantic_chunks(semantic):
    async def attempt(candidate, budget):
        if semantic:
            yield {"choices": [{"delta": {"content": "x" * 65537}}]}
        else:
            yield {"metadata": "x" * 65537}

    stream = InferenceRecovery(InferenceAvailability()).stream(
        plan(targets=1), attempt, deadline=time.monotonic() + 10
    )
    if semantic:
        assert len(await collect(stream)) == 1
    else:
        with pytest.raises(LLMStreamProviderError, match="prelude") as failure:
            await collect(stream)
        assert failure.value.to_payload()["recovery_exhausted"] is True
