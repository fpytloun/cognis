"""Controller-owned inference recovery; adapters execute one bounded attempt."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from cognis.core.redis_service import RedisUnavailableError
from cognis.providers.llm.availability import AvailabilityPermit, InferenceAvailability
from cognis.providers.llm.bindings import BindingSnapshot, InferenceBindings
from cognis.providers.llm.errors import (
    FastModeFallbackRequired,
    LLMStreamFailure,
    LLMStreamProviderError,
    MidStreamErrorPayload,
    OpenAIToolSearchFallbackRequired,
    classify_llm_exception,
)
from cognis.providers.llm.recovery_policy import RecoveryPolicy, RouteTarget
from cognis.providers.retry import compute_delay

logger = logging.getLogger(__name__)
active_attempt_budget: ContextVar[AttemptBudget | None] = ContextVar(
    "inference_attempt_budget", default=None
)

_RECOVERABLE = frozenset(
    {
        "rate_limit",
        "provider_5xx",
        "connection",
        "idle_timeout_raw",
        "idle_timeout_activity",
        "idle_timeout_reasoning",
        "other",
    }
)


class InferenceUnavailable(LLMStreamProviderError):
    """No permitted target can serve within the caller's recovery budget."""

    def __init__(self, retry_after_seconds: float = 0, *, category: str = "other") -> None:
        super().__init__(
            "No inference target is currently available",
            payload={
                "category": category,
                "message": "No inference target is currently available",
                "retry_after_seconds": retry_after_seconds,
            },
        )
        self.retry_after_seconds = retry_after_seconds


class IncompatibleCandidate(ValueError):
    """Preparation cannot faithfully represent this request on the target."""


@dataclass(frozen=True, slots=True)
class InferenceCandidate:
    target: RouteTarget
    fingerprint: str
    account_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class InferencePlan:
    """An authorized, immutable request plan; direct calls have one candidate."""

    reference: str
    owner: str
    candidates: tuple[InferenceCandidate, ...]
    policy: RecoveryPolicy
    policy_scope: str = "direct"
    session_identity: str | None = None
    binding_reference: str | None = None


@dataclass
class AttemptBudget:
    """One deadline and counter across transport retries and compatibility repairs."""

    deadline: float
    max_attempts: int
    attempts: int = 0
    recovery_until: float | None = None
    transport_calls: int = 0
    verify_dispatch: Any = None

    async def before_dispatch(self) -> None:
        if self.verify_dispatch is not None:
            await self.verify_dispatch()

    def before_transport(self) -> None:
        """Compatibility resubmissions consume the same allowance, never reset it."""
        if self.transport_calls >= self.attempts:
            self.consume()
        self.transport_calls = self.attempts

    def consume(self) -> None:
        if self.attempts >= self.max_attempts or (
            self.recovery_until is not None and time.monotonic() >= self.recovery_until
        ):
            raise InferenceUnavailable()
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Inference request deadline exceeded")
        self.attempts += 1

    @property
    def remaining(self) -> float:
        return max(0, self.deadline - time.monotonic())


def stream_failure(chunk: dict[str, Any]) -> LLMStreamProviderError | None:
    """Normalize explicit failed chunks before treating terminal markers as success."""
    if not (chunk.get("error") or chunk.get("response_error") or chunk.get("mid_stream_failure")):
        return None
    raw = chunk.get("response_error")
    payload: MidStreamErrorPayload
    if isinstance(raw, dict):
        payload = cast(
            MidStreamErrorPayload,
            {
                **raw,
                "category": str(raw.get("category") or "other"),
                "message": str(raw.get("message") or "Inference stream failed"),
            },
        )
    else:
        payload = classify_llm_exception(RuntimeError(str(chunk.get("error") or "Stream failed")))
    return LLMStreamProviderError("Inference provider stream failed", payload=payload)


def commits_output(chunk: dict[str, Any]) -> bool:
    """Semantic output includes native/replay state, not role-only SSE preludes."""
    if chunk.get("encoding") == "pcm_s16le" and chunk.get("type") in {
        "start",
        "audio",
        "partial",
        "transcript",
        "done",
    }:
        # A ready STT session must reach its microphone producer before any
        # transcript can exist. Never buffer speech behind text output.
        return True
    for event in chunk.get("anthropic_native_events") or []:
        block = event.get("content_block") or {}
        if event.get("type") in {"content_block_delta", "message_delta"} or (
            event.get("type") == "content_block_start"
            and (block.get("text") or block.get("type") not in {None, "text"})
        ):
            return True
    for key in (
        "_inference_result",
        "content",
        "text",
        "reasoning",
        "reasoning_content",
        "thinking_blocks",
        "tool_progress",
        "native_event",
        "response_output_item",
        "responses_output_item",
        "provider_thinking_blocks",
        "anthropic_native_envelope",
        "anthropic_native",
        "refusal",
    ):
        if chunk.get(key):
            return True
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or choice.get("message") or {}
        if any(
            delta.get(key)
            for key in (
                "content",
                "reasoning",
                "reasoning_content",
                "thinking_blocks",
                "provider_thinking_blocks",
                "tool_calls",
                "tool_progress",
                "function_call",
                "refusal",
            )
        ):
            return True
    return False


def _retry_after(payload: MidStreamErrorPayload) -> float:
    value = payload.get("retry_after_seconds")
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return max(0, value)
    return 0


class InferenceRecovery:
    """One transparent recovery loop shared by every operation and client."""

    def __init__(
        self, availability: InferenceAvailability, bindings: InferenceBindings | None = None
    ) -> None:
        self.availability = availability
        self.bindings = bindings

    async def stream(
        self,
        plan: InferencePlan,
        attempt: Callable[..., AsyncGenerator[dict[str, Any], None]],
        *,
        deadline: float,
        request_id: str | None = None,
        replay_safe: bool = True,
        prepare: Callable[[InferenceCandidate, AttemptBudget], Awaitable[Any]] | None = None,
        failback: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Enforce deadlines even when clients resume an iterator in new tasks."""
        deadline = min(deadline, time.monotonic() + plan.policy.request_timeout_seconds)
        async with aclosing(
            self._recover(
                plan,
                attempt,
                deadline=deadline,
                request_id=request_id,
                replay_safe=replay_safe,
                prepare=prepare,
                failback=failback,
            )
        ) as iterator:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Inference request deadline exceeded")
                try:
                    chunk = await asyncio.wait_for(anext(iterator), remaining)
                except StopAsyncIteration:
                    return
                yield chunk

    async def _recover(
        self,
        plan: InferencePlan,
        attempt: Callable[..., AsyncGenerator[dict[str, Any], None]],
        *,
        deadline: float,
        request_id: str | None = None,
        replay_safe: bool = True,
        prepare: Callable[[InferenceCandidate, AttemptBudget], Awaitable[Any]] | None = None,
        failback: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Recover before semantic commitment, never concatenate two generations."""
        request_id = request_id or uuid.uuid4().hex
        policy = plan.policy
        deadline = min(deadline, time.monotonic() + policy.request_timeout_seconds)
        claim: BindingSnapshot | None = None
        binding: BindingSnapshot | None = None
        bindings = self.bindings
        if not plan.candidates:
            raise ValueError("Inference plan has no authorized candidates")

        async with asyncio.timeout_at(deadline):
            if bindings is not None and plan.session_identity:
                binding = await bindings.get_or_create(
                    owner=plan.owner,
                    session_identity=plan.session_identity,
                    reference=plan.binding_reference or plan.reference,
                )
            visited: set[str] = set()
            deferred: set[str] = set()
            incompatible: set[str] = set()
            attempts_used: dict[str, int] = {}
            switches = 0
            last_error: Exception | None = None
            while len(visited) < len(plan.candidates):
                if binding is not None:
                    assert bindings is not None
                    binding = await bindings.read(binding.key)
                    if binding.reserved:
                        await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
                        continue
                candidates = [
                    candidate
                    for candidate in plan.candidates
                    if candidate.target.reference not in visited
                ]
                random.shuffle(candidates)
                candidates.sort(key=lambda c: c.target.priority)
                if binding is not None and binding.target is not None:
                    bound_reference = binding.target.reference
                    candidates.sort(key=lambda c: c.target.reference != bound_reference)
                    if failback and not visited:
                        higher = [
                            item
                            for item in candidates
                            if item.target.priority < binding.target.priority
                        ]
                        if higher:
                            best = min(higher, key=lambda item: item.target.priority)
                            candidates.remove(best)
                            candidates.insert(0, best)
                selected: InferenceCandidate | None = None
                permit: AvailabilityPermit | None = None
                blocked: list[tuple[float, str | None]] = []
                for candidate in candidates:
                    admission, delay, category = await self._admit(candidate, plan, deadline)
                    if not delay:
                        permit = admission
                        selected = candidate
                        break
                    blocked.append((delay, category))
                if selected is None or permit is None:
                    if deferred:
                        # Unvisited candidates can all be preblocked. Include
                        # earlier reset failures before choosing which wait wins.
                        visited.difference_update(deferred)
                        deferred.clear()
                        continue
                    delay, category = min(blocked, key=lambda item: item[0], default=(0, None))
                    if delay > 0 and delay < deadline - time.monotonic():
                        await asyncio.sleep(delay)
                        continue
                    raise self._unavailable(last_error, delay, category)
                candidate = selected
                if binding is not None and (
                    binding.target is None or binding.target.reference != candidate.target.reference
                ):
                    assert bindings is not None
                    try:
                        claim = await bindings.reserve(
                            binding,
                            candidate.target,
                            lease_seconds=max(1, deadline - time.monotonic()) + 5,
                        )
                    except BaseException:
                        await self._report("release", permit, policy)
                        raise
                    if claim is None:
                        await self._report("release", permit, policy)
                        continue
                visited.add(candidate.target.reference)
                budget = AttemptBudget(
                    deadline, policy.max_retries_per_target + 1 if replay_safe else 1
                )
                budget.attempts = attempts_used.get(candidate.target.reference, 0)
                if (
                    failback
                    and binding is not None
                    and binding.target is not None
                    and (candidate.target.priority < binding.target.priority)
                ):
                    budget.max_attempts = 1

                async def verify_dispatch(
                    selected: InferenceCandidate = candidate,
                    admission: AvailabilityPermit | None = permit,
                    reserved: BindingSnapshot | None = claim,
                    bound: BindingSnapshot | None = binding,
                ) -> None:
                    assert admission is not None
                    if await self._limit_delay(selected) or not await self.availability.verify(
                        admission, policy
                    ):
                        raise InferenceUnavailable()
                    if reserved is not None:
                        assert bindings is not None
                        current = await bindings.read(reserved.key)
                        if current.claim_token != reserved.claim_token or not current.reserved:
                            raise RuntimeError("Inference replacement claim expired")
                    elif bound is not None:
                        await self._verify_selection(bound)

                budget.verify_dispatch = verify_dispatch
                committed = False
                try:
                    while budget.attempts < budget.max_attempts:
                        if (
                            budget.recovery_until is not None
                            and time.monotonic() >= budget.recovery_until
                        ):
                            await self._report(
                                "failure",
                                permit,
                                policy,
                                request_id=request_id,
                                failure_category=(
                                    classify_llm_exception(last_error).get("category")
                                    if last_error
                                    else None
                                ),
                            )
                            break
                        try:
                            prepared = await prepare(candidate, budget) if prepare else None
                        except IncompatibleCandidate as exc:
                            # A rejected fallback must not hide the upstream
                            # rate limit/outage that triggered recovery.
                            last_error = last_error or exc
                            incompatible.add(candidate.target.reference)
                            break
                        budget.consume()
                        prelude: list[dict[str, Any]] = []
                        prelude_bytes = 0
                        try:
                            call = (
                                attempt(candidate, budget, prepared)
                                if prepare
                                else attempt(candidate, budget)
                            )
                            async with aclosing(call) as stream:
                                async for chunk in stream:
                                    if "_inference_selection" in chunk:
                                        # Control-plane metadata is not provider output.
                                        yield chunk
                                        continue
                                    failure = stream_failure(chunk)
                                    if failure is not None:
                                        raise failure
                                    if not committed:
                                        if not commits_output(chunk):
                                            prelude_bytes += len(json_bytes(chunk))
                                            if prelude_bytes > 65536:
                                                raise ValueError("Inference prelude exceeds limit")
                                            prelude.append(chunk)
                                            continue
                                        if claim is not None:
                                            assert bindings is not None
                                            if not await bindings.commit(claim):
                                                raise RuntimeError(
                                                    "Inference binding claim expired"
                                                )
                                            claim = None
                                        elif binding is not None:
                                            await self._verify_selection(binding)
                                        committed = True
                                        for buffered in prelude:
                                            yield buffered
                                        prelude.clear()
                                        yield chunk
                                    else:
                                        yield chunk
                            if not committed:
                                if claim is not None:
                                    assert bindings is not None
                                    if not await bindings.commit(claim):
                                        raise RuntimeError("Inference binding claim expired")
                                    claim = None
                                elif binding is not None:
                                    await self._verify_selection(binding)
                                for buffered in prelude:
                                    yield buffered
                            await self._report("success", permit, policy)
                            return
                        except (FastModeFallbackRequired, OpenAIToolSearchFallbackRequired):
                            # These signals request a changed provider request,
                            # not an identical transport retry or route fallback.
                            raise
                        except Exception as exc:
                            if (
                                isinstance(exc, (IncompatibleCandidate, InferenceUnavailable))
                                and not committed
                            ):
                                last_error = last_error or exc
                                if isinstance(exc, IncompatibleCandidate):
                                    incompatible.add(candidate.target.reference)
                                break
                            last_error = exc
                            payload = classify_llm_exception(exc)
                            category = payload.get("category")
                            recoverable = category in _RECOVERABLE
                            quota = category == "quota_exhausted"
                            reset_after = _retry_after(payload)
                            if quota or (category == "rate_limit" and reset_after):
                                limit_key = (
                                    candidate.account_fingerprint
                                    if quota
                                    else candidate.fingerprint
                                )
                                if limit_key:
                                    try:
                                        await self.availability.limit(
                                            limit_key,
                                            block_seconds=reset_after or policy.cooldown_seconds,
                                        )
                                    except RedisUnavailableError:
                                        logger.warning("Unable to report shared inference limit")
                            if committed:
                                if recoverable or quota:
                                    await self._report(
                                        "failure",
                                        permit,
                                        policy,
                                        request_id=request_id,
                                        retry_after_seconds=_retry_after(payload),
                                        failure_category=str(category),
                                    )
                                raise self._exhausted(exc) from exc
                            if replay_safe and category == "authentication":
                                # Rejected credentials need the user, so retrying this
                                # target cannot help and is not an outage either. Another
                                # route target may still serve the request.
                                break
                            if not replay_safe or not (recoverable or quota):
                                raise self._exhausted(exc) from exc
                            now = time.monotonic()
                            if budget.recovery_until is None:
                                budget.recovery_until = now + policy.target_recovery_window_seconds
                            delay = _retry_after(payload) or compute_delay(
                                budget.attempts - 1, 1, 30, True
                            )
                            if (
                                quota
                                or budget.attempts >= budget.max_attempts
                                or now + delay >= min(deadline, budget.recovery_until)
                            ):
                                if reset_after and budget.attempts < budget.max_attempts:
                                    deferred.add(candidate.target.reference)
                                await self._report(
                                    "failure",
                                    permit,
                                    policy,
                                    request_id=request_id,
                                    retry_after_seconds=_retry_after(payload),
                                    failure_category=str(category),
                                )
                                break
                            await asyncio.sleep(delay)
                            # A concurrent outage must be observed before another attempt.
                            # A half-open trial keeps its exclusive permit for this budget.
                            if not permit.trial_token:
                                permit = await self.availability.admit(
                                    permit.key,
                                    policy,
                                    lease_seconds=max(1, deadline - time.monotonic()),
                                )
                                if not permit.allowed:
                                    break
                finally:
                    attempts_used[candidate.target.reference] = budget.attempts
                    if claim is not None:
                        assert bindings is not None
                        await bindings.release(claim)
                        claim = None
                    await self._report("release", permit, policy)
                switches += 1
                if switches > policy.max_target_switches:
                    break
                if len(visited) == len(plan.candidates) and deferred:
                    # Re-admit known reset candidates only after all other
                    # targets have been considered. Total physical attempts and
                    # route switches remain bounded across these wait rounds.
                    visited.difference_update(deferred)
                    deferred.clear()
            # Recompute the route retry time, including targets attempted in
            # this call, rather than forwarding the last provider's longer wait.
            blocked = []
            for candidate in plan.candidates:
                if candidate.target.reference in incompatible:
                    continue
                admission, delay, category = await self._admit(candidate, plan, deadline)
                blocked.append((delay, category))
                if not delay:
                    await self._report("release", admission, policy)
            route_delay, category = min(blocked, key=lambda item: item[0], default=(None, None))
            raise self._unavailable(last_error, route_delay, category)

    @staticmethod
    def _exhausted(exc: Exception) -> Exception:
        payload = classify_llm_exception(exc)
        payload["recovery_exhausted"] = True
        if isinstance(exc, LLMStreamFailure):
            exc.payload = payload
            return exc
        return LLMStreamProviderError(str(exc), payload=payload)

    @staticmethod
    def _unavailable(
        last_error: Exception | None, delay: float | None, category: str | None
    ) -> Exception:
        delay = math.ceil(delay) if delay is not None else None
        if last_error is not None and not isinstance(
            last_error, (InferenceUnavailable, IncompatibleCandidate)
        ):
            if delay is not None:
                return LLMStreamProviderError(
                    str(last_error),
                    payload={
                        **classify_llm_exception(last_error),
                        "retry_after_seconds": delay,
                        "recovery_exhausted": True,
                    },
                )
            return InferenceRecovery._exhausted(last_error)
        if category:
            return InferenceRecovery._exhausted(InferenceUnavailable(delay or 0, category=category))
        return InferenceRecovery._exhausted(last_error or InferenceUnavailable(delay or 0))

    async def _admit(
        self, candidate: InferenceCandidate, plan: InferencePlan, deadline: float
    ) -> tuple[AvailabilityPermit, float, str | None]:
        """Use the longest blocker per target and the caller picks the earliest target."""
        limits = [
            (await self.availability.limit(key), category)
            for key, category in (
                (candidate.fingerprint, "rate_limit"),
                (candidate.account_fingerprint, "quota_exhausted"),
            )
            if key
        ]
        delay, category = max(limits, key=lambda item: item[0], default=(0, None))
        if not delay:
            category = None
        key = self.availability.key(
            target_fingerprint=candidate.fingerprint, policy=plan.policy, scope=plan.policy_scope
        )
        permit = await self.availability.admit(
            key, plan.policy, lease_seconds=max(1, deadline - time.monotonic())
        )
        if permit.retry_after_seconds > delay:
            delay = permit.retry_after_seconds
            category = permit.failure_category or category
        if delay:
            await self._report("release", permit, plan.policy)
        return permit, delay, category

    async def _limit_delay(self, candidate: InferenceCandidate) -> float:
        delays = [
            await self.availability.limit(key)
            for key in {candidate.fingerprint, candidate.account_fingerprint}
            if key
        ]
        return max(delays, default=0)

    async def _verify_selection(self, binding: BindingSnapshot) -> None:
        """Do not publish a stale target after concurrent pre-output failover."""
        assert self.bindings is not None
        current = await self.bindings.read(binding.key)
        if current.revision != binding.revision or current.reserved:
            raise RuntimeError("Inference target changed before output commitment")

    async def _report(
        self,
        outcome: str,
        permit: AvailabilityPermit,
        policy: RecoveryPolicy,
        *,
        request_id: str = "",
        retry_after_seconds: float = 0,
        failure_category: str | None = None,
    ) -> None:
        try:
            if outcome == "failure":
                await self.availability.failed(
                    permit,
                    policy,
                    request_id=request_id,
                    retry_after_seconds=retry_after_seconds,
                    failure_category=failure_category,
                )
            elif outcome == "success":
                await self.availability.succeeded(permit, policy)
            else:
                await self.availability.release(permit, policy)
        except RedisUnavailableError:
            # Reporting must not convert a completed generation into a retry.
            # The next admission still fails closed.
            logger.warning("Inference availability report failed", extra={"outcome": outcome})


def json_bytes(chunk: dict[str, Any]) -> bytes:
    """Bound pre-output buffering even when adapters emit many metadata chunks."""
    import json

    return json.dumps(chunk, default=str).encode()
