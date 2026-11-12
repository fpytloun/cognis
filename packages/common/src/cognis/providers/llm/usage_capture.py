"""Request-owned, nonblocking inference observations shared by both runtimes.

The only side effect is a synchronous sink offer. There is no task, SQL, capacity
wait, global request context or inference retry here. Queue acceptance is not
durability. The owner of inference cancellation also owns stream closure.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from cognis.models.config import ModelInfo
from cognis.models.usage import (
    USAGE_CONTEXT_TARGET_BYTES,
    USAGE_OBSERVATION_MAX_BYTES,
    USAGE_TERMINAL_RESERVE_BYTES,
    UsageAttemptContext,
    UsageAttemptFinalized,
    UsageAttemptSink,
    UsageAttemptStarted,
    UsageAttribution,
    UsageExecutorContext,
    UsageHTTPMetadata,
    UsageId,
    UsageMetadataReduction,
    UsageModelName,
    UsageModelParameters,
    UsageModelSelection,
    UsageObservation,
    UsageOperation,
    UsageOutcome,
    UsagePerformance,
    UsageSafeError,
    UsageServiceTierApplicability,
    UsageTrace,
)
from cognis.models.usage_pricing import (
    UsageExecutorPriceCandidate,
    UsagePricingLookup,
    UsagePricingPin,
    normalize_usage_service_tier,
    usage_operation_matches_scope,
    usage_pricing_fingerprint,
)
from cognis.providers.llm.usage_normalization import UsageAccumulator, field, meaningful, safe_error
from cognis.providers.llm.usage_parameters import project_parameters

_ID = TypeAdapter(UsageId)
_MODEL = TypeAdapter(UsageModelName)


def optional_id(value: Any) -> str | None:
    """Reject invalid optional metadata without modifying the inference request."""
    try:
        return _ID.validate_python(value, strict=True) if value is not None else None
    except ValidationError:
        return None


def optional_model(value: Any) -> str | None:
    """Model names have an independent printable-Unicode bound, not an ID bound."""
    try:
        return _MODEL.validate_python(value, strict=True) if value is not None else None
    except ValidationError:
        return None


class UsageCapture:
    """Explicit process-lifetime observer, with bounded diagnostic cardinality."""

    def __init__(
        self,
        *,
        sink: UsageAttemptSink,
        controller_owner_id: str,
        coverage_epoch_id: str,
        price_lookup: UsagePricingLookup | None = None,
    ) -> None:
        offer = getattr(sink, "try_emit", None)
        if not callable(offer) or inspect.iscoroutinefunction(offer):
            raise TypeError("Usage capture requires a configured synchronous observation sink")
        self.sink = sink
        self.controller_owner_id = _ID.validate_python(controller_owner_id, strict=True)
        self.coverage_epoch_id = _ID.validate_python(coverage_epoch_id, strict=True)
        self.price_lookup = price_lookup
        self.rejected_events = 0
        self.failed_events = 0
        self.reduced_contexts = 0

    def pricing_pin(
        self,
        *,
        provider_id: str,
        model: str,
        model_info: ModelInfo | None,
        provider_config: Mapping[str, Any] | None,
        service_tier: Any,
        applicability: UsageServiceTierApplicability | None = None,
    ) -> UsagePricingPin | None:
        """Resolve committed identities only for a known concrete dispatch tier."""
        if (
            self.price_lookup is None
            or model_info is None
            or provider_config is None
            or (
                not (applicability is not None and applicability.state == "not_applicable")
                and (
                    not isinstance(service_tier, str)
                    or not service_tier.strip()
                    or service_tier != service_tier.strip()
                    or service_tier.casefold() in {"auto", "unknown"}
                )
            )
        ):
            return None
        try:
            fingerprint = usage_pricing_fingerprint(
                provider_id=provider_id,
                model=model,
                model_info=model_info,
                provider_config=provider_config,
                service_tier=service_tier,
                applicability=applicability,
            )
            pin = self.price_lookup(
                provider_id=provider_id,
                model=model,
                metadata_fingerprint=fingerprint,
            )
            if pin is None:
                return None
            if inspect.iscoroutine(pin):
                pin.close()
                raise TypeError("Pricing lookup must be synchronous")
            if not isinstance(pin, UsagePricingPin):
                raise TypeError("Pricing lookup must return a committed pin")
            # The protocol is typed, but an invalid adapter must not drop the
            # entire attempt or silently turn a malformed price ref into a hit.
            snapshot = _ID.validate_python(pin.resolver_snapshot_id, strict=True)
            revision = (
                _ID.validate_python(pin.captured_price_revision_id, strict=True)
                if pin.captured_price_revision_id is not None
                else None
            )
            return UsagePricingPin(revision, snapshot)
        except Exception:
            self.failed_events += 1
            return None

    def emit(self, observation: UsageObservation) -> bool:
        """Offer once; broken instrumentation cannot fail or replay inference."""
        try:
            accepted = self.sink.try_emit(observation)
        except Exception:
            self.failed_events += 1
            return False
        if accepted is not True:
            self.rejected_events += 1
            return False
        return True

    def request(
        self,
        *,
        attribution: UsageAttribution,
        request_id: str | None = None,
        cycle_id: str | None = None,
        requested_model: str | None = None,
        requested_parameters: UsageModelParameters | None = None,
    ) -> UsageRequest:
        """Create a request-local attempt trail, never process-global state."""
        identity = optional_id(request_id) or uuid.uuid4().hex
        return UsageRequest(
            capture=self,
            attribution=attribution,
            request_id=identity,
            cycle_id=optional_id(cycle_id) or identity,
            requested_model=optional_model(requested_model),
            requested_parameters=requested_parameters or UsageModelParameters(),
        )


class UsageRequest:
    """One logical request's correlation and physical-attempt ordering."""

    def __init__(
        self,
        *,
        capture: UsageCapture,
        attribution: UsageAttribution,
        request_id: str,
        cycle_id: str,
        requested_model: str | None,
        requested_parameters: UsageModelParameters,
    ) -> None:
        self.capture = capture
        self.attribution = attribution
        self.started_at = datetime.now(UTC)
        self.http_metadata: UsageHTTPMetadata | None = None
        self.gateway_outcome: Literal["success", "error", "cancelled"] | None = None
        self.request_id = request_id
        self.cycle_id = cycle_id
        self.requested_model = requested_model
        self.requested_parameters = requested_parameters
        self.ordinal = 0
        self.previous: UsageAttemptContext | None = None
        self.remote_event_ids: set[str] = set()
        self.executor_price_candidates: dict[str, tuple[UsageExecutorPriceCandidate, ...]] = {}
        self.first_generated_at: datetime | None = None
        self.first_generated_monotonic: float | None = None

    def mark_generated(self) -> None:
        """Mark meaningful output observed on this request's local clock."""
        if self.first_generated_at is None:
            self.first_generated_at = datetime.now(UTC)
            self.first_generated_monotonic = time.monotonic()

    def begin(
        self,
        *,
        selection: UsageModelSelection,
        executor: UsageExecutorContext | None = None,
        clock_domain: Literal["controller", "executor"] = "controller",
        semantic_profile: Literal["openai", "anthropic", "unknown"] = "unknown",
        emit_start: bool = True,
        attribution: UsageAttribution | None = None,
    ) -> UsageAttempt:
        """Allocate a new physical ID synchronously immediately before dispatch."""
        self.ordinal += 1
        previous = self.previous
        fallback = previous is not None and (
            previous.selection.provider_id != selection.provider_id
            or previous.selection.effective_model != selection.effective_model
            or previous.selection.operation != selection.operation
        )
        context = UsageAttemptContext(
            trace=UsageTrace(
                request_id=self.request_id,
                cycle_id=self.cycle_id,
                attempt_id=uuid.uuid4().hex,
                attempt_ordinal=self.ordinal,
                retry_of_attempt_id=previous.trace.attempt_id
                if previous and not fallback
                else None,
                fallback_from_attempt_id=previous.trace.attempt_id
                if previous and fallback
                else None,
            ),
            attribution=attribution or self.attribution,
            selection=selection,
            started_at=datetime.now(UTC),
            request_started_at=self.started_at,
            controller_owner_id=self.capture.controller_owner_id,
            coverage_epoch_id=self.capture.coverage_epoch_id,
            executor=executor,
        )
        context = self._fit_context(context)
        self.previous = context
        return UsageAttempt(
            capture=self.capture,
            context=context,
            clock_domain=clock_domain,
            semantic_profile=semantic_profile,
            on_generated=self.mark_generated,
            emit_start=emit_start,
        )

    def _fit_context(self, context: UsageAttemptContext) -> UsageAttemptContext:
        # Reserve space for terminal counts, timing and safe errors. A final
        # event repeats exactly this frozen context even if its start is lost.
        budget = min(
            USAGE_CONTEXT_TARGET_BYTES,
            USAGE_OBSERVATION_MAX_BYTES - USAGE_TERMINAL_RESERVE_BYTES,
        )

        def fits(value: UsageAttemptContext) -> bool:
            return (
                len(value.model_dump_json(exclude_none=True, exclude_defaults=True).encode())
                <= budget
            )

        if fits(context):
            return context
        self.capture.reduced_contexts += 1
        fields: list[Any] = list(
            context.metadata_reduction.fields if context.metadata_reduction is not None else (),
        )

        def declare(value: UsageAttemptContext) -> UsageAttemptContext:
            return value.model_copy(
                update={
                    "metadata_reduction": UsageMetadataReduction(fields=tuple(fields)),
                }
            )

        if context.attribution.client_labels is not None:
            fields.append("client_labels")
            context = declare(
                context.model_copy(
                    update={
                        "attribution": context.attribution.model_copy(
                            update={"client_labels": None}
                        ),
                    }
                )
            )
            if fits(context):
                return context
        hook = context.attribution.hook
        if hook is not None and hook.hook_name is not None:
            fields.append("hook_display_name")
            context = declare(
                context.model_copy(
                    update={
                        "attribution": context.attribution.model_copy(
                            update={
                                "hook": hook.model_copy(update={"hook_name": None}),
                            }
                        ),
                    }
                )
            )
            if fits(context):
                return context
        # Configuration scalars and their semantic sources are lossless contract
        # data, not optional padding. Never erase them to make an envelope fit.
        # Verified resources, identities, models and trace fields are never padding.
        # An impossible envelope remains a diagnosed gap, never inference failure.
        return context

    def selection(
        self,
        *,
        provider_id: str,
        model: str,
        operation: UsageOperation,
        streaming: bool,
        wire_parameters: Mapping[str, Any] | UsageModelParameters,
        source: Literal["provider", "native_sdk", "adapter"] = "adapter",
        model_info: ModelInfo | None = None,
        provider_config: Mapping[str, Any] | None = None,
        applicability: UsageServiceTierApplicability | None = None,
        tier_omitted: bool = False,
    ) -> UsageModelSelection:
        """Build safe normalized evidence, not a copy of outgoing kwargs.

        ``tier_omitted`` carries omission proof for callers that pass already
        projected parameters, which no longer show whether a tier was sent.
        """
        effective = (
            wire_parameters
            if isinstance(wire_parameters, UsageModelParameters)
            else project_parameters(wire_parameters, source="adapter_override")
        )
        # Submitted native-label evidence, not ModelInfo's metadata default or
        # tariff applicability. Storage owns native-label normalization.
        tier = effective.service_tier
        if (
            applicability is not None
            and applicability.state == "not_applicable"
            and model_info is not None
            and model_info.untiered_tariff is None
        ):
            # Without a declared untiered tariff, a tierless resource (for example
            # embeddings) bills at the model's single default-tier price.
            applicability = None
        if applicability is not None and (
            applicability.state != "not_applicable"
            or applicability.scope is None
            or not usage_operation_matches_scope(operation, applicability.scope)
            or tier is not None
        ):
            applicability = None
        if tier is not None and (
            not tier or tier != tier.strip() or tier.casefold() in {"auto", "unknown"}
        ):
            tier = None
        if (
            tier is None
            and applicability is None
            and model_info is not None
            and model_info.model_mode
            not in {"image_generation", "audio_speech", "audio_transcription", "video"}
            and (
                tier_omitted
                or (
                    isinstance(wire_parameters, Mapping)
                    and not {"service_tier", "speed"} & wire_parameters.keys()
                )
            )
        ):
            # A request naming no tier is billed at the model's default tier. Only a
            # provably omitted parameter qualifies; dropped values stay unknown.
            tier = normalize_usage_service_tier(model_info.tier)
        pin = self.capture.pricing_pin(
            provider_id=provider_id,
            model=model,
            model_info=model_info,
            provider_config=provider_config,
            service_tier=tier,
            applicability=applicability,
        )
        return UsageModelSelection(
            provider_id=provider_id,
            effective_model=model,
            requested_model=self.requested_model,
            requested_route=optional_id(self.requested_model)
            if self.requested_model and self.requested_model.startswith("routes/")
            else None,
            operation=operation,
            streaming=streaming,
            source=source,
            service_tier=tier,
            service_tier_applicability=applicability,
            requested_parameters=self.requested_parameters,
            captured_price_revision_id=pin.captured_price_revision_id if pin else None,
            resolver_snapshot_id=pin.resolver_snapshot_id if pin else None,
            effective_parameters=effective,
        )

    async def call(
        self,
        dispatch: Callable[[], Awaitable[Any]],
        *,
        selection: UsageModelSelection,
        semantic_profile: Literal["openai", "anthropic", "unknown"] = "unknown",
        attribution: UsageAttribution | None = None,
    ) -> Any:
        """Observe one physical invocation, including returned streaming lifetime."""
        try:
            attempt = self.begin(
                selection=selection,
                semantic_profile=semantic_profile,
                attribution=attribution,
            )
        except Exception:
            self.capture.failed_events += 1
            return await dispatch()
        try:
            result = await dispatch()
        except BaseException as exc:
            attempt.finish(error=exc)
            raise
        if hasattr(result, "__aiter__") and selection.streaming:
            return ObservedStream(result, attempt)
        attempt.observe(result, generated=False)
        if meaningful(result):
            self.mark_generated()
        attempt.finish()
        return result


class UsageAttempt:
    """In-memory attempt state; terminal observation is emitted at most once."""

    def __init__(
        self,
        *,
        capture: UsageCapture,
        context: UsageAttemptContext,
        clock_domain: Literal["controller", "executor"],
        semantic_profile: Literal["openai", "anthropic", "unknown"],
        on_generated: Callable[[], None] | None = None,
        emit_start: bool = True,
    ) -> None:
        self.capture = capture
        self.context = context
        self.clock_domain = clock_domain
        self.accumulator = UsageAccumulator(semantic_profile, operation=context.selection.operation)
        self.started = time.monotonic()
        self.started_wall = datetime.now(UTC)
        self.first: float | None = None
        self.last: float | None = None
        self.first_at: datetime | None = None
        self.last_at: datetime | None = None
        self.finished = False
        self.observed_failure = False
        self.on_generated = on_generated
        try:
            if emit_start:
                capture.emit(
                    UsageAttemptStarted(
                        observation_id=f"{context.trace.attempt_id}:started",
                        context=context,
                    )
                )
        except Exception:
            capture.failed_events += 1

    def observe(
        self,
        value: Any,
        *,
        generated: bool = True,
        semantic_profile: Literal["openai", "anthropic", "unknown"] | None = None,
    ) -> None:
        """Consume only numeric/identity fields and a meaningful-generation flag."""
        try:
            if semantic_profile is not None:
                self.accumulator.semantic_profile = semantic_profile
            self.accumulator.observe(value)
            nested = field(value, "response") or value
            if (
                field(value, "type") == "response.failed"
                or field(nested, "status") == "failed"
                or field(value, "error")
            ):
                self.observed_failure = True
            if generated and meaningful(value):
                self.mark_generated()
        except Exception:
            self.capture.failed_events += 1

    def mark_generated(self) -> None:
        """Record meaningful generation without replacing raw provider usage."""
        if self.finished:
            return
        try:
            if self.on_generated is not None:
                self.on_generated()
            now = time.monotonic()
            observed_at = datetime.now(UTC)
            if self.first is None:
                self.first, self.first_at = now, observed_at
            self.last, self.last_at = now, observed_at
        except Exception:
            self.capture.failed_events += 1

    def finish(
        self, *, error: BaseException | None = None, outcome: UsageOutcome = "success"
    ) -> None:
        """Offer final data without an await; telemetry cannot change the result."""
        if self.finished:
            return
        self.finished = True
        if error is not None:
            outcome = (
                "cancelled"
                if isinstance(error, (asyncio.CancelledError, GeneratorExit))
                else "error"
            )
        elif self.observed_failure and outcome == "success":
            outcome = "error"
        try:
            self.capture.emit(
                UsageAttemptFinalized(
                    observation_id=f"{self.context.trace.attempt_id}:finalized",
                    context=self.context,
                    outcome=outcome,
                    dispatch_status="dispatch_observed",
                    finalized_at=datetime.now(UTC),
                    performance=UsagePerformance(
                        started_at=self.started_wall,
                        ended_at=datetime.now(UTC),
                        total_elapsed_ms=max(0, time.monotonic() - self.started) * 1000,
                        ttft_ms=(self.first - self.started) * 1000
                        if self.first is not None
                        else None,
                        first_generated_at=self.first_at,
                        last_generated_at=self.last_at,
                        generation_elapsed_ms=(self.last - self.first) * 1000
                        if self.last is not None and self.first is not None
                        else None,
                        source="adapter",
                        clock_domain=self.clock_domain,
                    ),
                    usage=self.accumulator.normalized(
                        partial=outcome != "success",
                        input_only=self.context.selection.operation == "embeddings",
                    ),
                    provider_request_id=optional_id(self.accumulator.request_id),
                    provider_reported_model=optional_model(self.accumulator.reported_model),
                    error=safe_error(error)
                    if error is not None
                    else UsageSafeError(
                        category="unknown",
                        summary_code="provider_error",
                    )
                    if self.observed_failure
                    else None,
                )
            )
        except Exception:
            self.capture.failed_events += 1


class ObservedStream:
    """Close-aware stream preserving original objects and exactly-once terminal offer."""

    def __init__(self, stream: AsyncIterator[Any], attempt: UsageAttempt) -> None:
        self.source = stream
        self.stream = aiter(stream)
        self.attempt = attempt
        self.closed = False

    def __aiter__(self) -> ObservedStream:
        return self

    async def __anext__(self) -> Any:
        try:
            value = await anext(self.stream)
        except StopAsyncIteration:
            self.attempt.finish()
            raise
        except BaseException as exc:
            self.attempt.finish(error=exc)
            raise
        self.attempt.observe(value)
        return value

    async def aclose(self) -> None:
        """Close upstream resources even when no first chunk was consumed."""
        if self.closed:
            return
        self.closed = True
        try:
            close = getattr(self.stream, "aclose", None) or getattr(self.stream, "close", None)
            if close is not None:
                await close()
            if self.source is not self.stream:
                close_source = getattr(self.source, "aclose", None) or getattr(
                    self.source, "close", None
                )
                if close_source is not None:
                    await close_source()
        except BaseException as exc:
            self.attempt.finish(error=exc)
            raise
        finally:
            self.attempt.finish(error=GeneratorExit())

    async def close(self) -> None:
        """Preserve the OpenAI SDK's async close interface as well as aclose."""
        await self.aclose()
