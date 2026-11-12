"""Executor correlation and ingestion on the originating controller only."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import TypeAdapter

from cognis.models.usage import (
    UsageAttemptContext,
    UsageAttemptFinalized,
    UsageAttemptStarted,
    UsageExecutorContext,
    UsageObservation,
    UsageServiceTierApplicability,
)
from cognis.models.usage_pricing import UsageExecutorPriceCandidate, usage_pricing_fingerprint
from cognis.providers.llm.usage_binding import UsageBinding

_OBSERVATION: TypeAdapter[UsageObservation] = TypeAdapter(UsageObservation)
CONTEXT_KEY = "cognis_usage_context"
OBSERVATIONS_KEY = "cognis_usage_observations"


def executor_metadata(
    binding: UsageBinding | None,
    metadata: dict[str, Any] | None,
    *,
    executor_id: str | None,
    parameters: dict[str, Any],
    streaming: bool,
) -> tuple[dict[str, Any], UsageAttemptContext | None]:
    """Allocate correlation without recording an outer controller attempt."""
    result = dict(metadata or {})
    if binding is None:
        return result, None
    if executor_id is None:
        binding.request.capture.failed_events += 1
        return result, None
    try:
        selection = binding.request.selection(
            provider_id=binding.provider_id,
            model=binding.model,
            operation="native",
            streaming=streaming,
            wire_parameters=parameters,
        )
        correlation = UsageExecutorContext(
            executor_id=executor_id,
            controller_owner_id=binding.request.capture.controller_owner_id,
            inference_request_id=uuid.uuid4().hex,
        )
        # A finite request-owned snapshot is frozen before dispatch. It is never
        # serialized as caller metadata or refreshed when observations return.
        info = binding.model_info
        declaration = info.untiered_tariff if info is not None else None
        if (
            declaration is not None
            and info is not None
            and binding.provider_config is not None
            and len(binding.request.executor_price_candidates) < 32
        ):
            evidence = UsageServiceTierApplicability(
                state="not_applicable", scope=declaration.scope
            )
            try:
                fingerprint = usage_pricing_fingerprint(
                    provider_id=binding.provider_id,
                    model=binding.model,
                    model_info=info,
                    provider_config=binding.provider_config,
                    applicability=evidence,
                )
            except ValueError:
                binding.request.capture.failed_events += 1
            else:
                pin = binding.request.capture.pricing_pin(
                    provider_id=binding.provider_id,
                    model=binding.model,
                    model_info=info,
                    provider_config=binding.provider_config,
                    service_tier=None,
                    applicability=evidence,
                )
                if pin is not None and pin.resolver_snapshot_id != fingerprint:
                    pin = None
                binding.request.executor_price_candidates[correlation.inference_request_id] = (
                    UsageExecutorPriceCandidate(
                        binding.provider_id, binding.model, declaration.scope, fingerprint, pin
                    ),
                )
        allocated = binding.request.begin(
            attribution=binding.attribution(),
            selection=selection,
            executor=correlation,
            emit_start=False,
        ).context
        result[CONTEXT_KEY] = allocated.model_dump(mode="json", exclude_none=True)
        if binding.media_operation is not None:
            result["cognis_media_usage_version"] = 1
            result["cognis_media_operation"] = binding.media_operation
            selected = binding.request.selection(
                provider_id=binding.provider_id,
                model=binding.model,
                operation=binding.media_operation,
                streaming=streaming,
                wire_parameters=parameters,
                model_info=binding.model_info,
                provider_config=binding.provider_config,
            )
            allocated = allocated.model_copy(update={"selection": selected})
        return result, allocated
    except Exception:
        binding.request.capture.failed_events += 1
        return result, None


def ingest_executor(
    result: dict[str, Any],
    binding: UsageBinding | None,
    expected: UsageAttemptContext | None,
) -> dict[str, Any]:
    """Only accept observations matching the immutable originating context."""
    metadata = result.get("backend_metadata")
    if not isinstance(metadata, dict) or OBSERVATIONS_KEY not in metadata:
        return result
    metadata = dict(metadata)
    offered = metadata.pop(OBSERVATIONS_KEY)
    result = {key: value for key, value in result.items() if key != "backend_metadata"}
    if metadata:
        result["backend_metadata"] = metadata
    if binding is None or expected is None:
        return result
    if not isinstance(offered, list) or len(offered) > 32:
        binding.request.capture.failed_events += 1
        return result
    for value in offered:
        try:
            if isinstance(value, dict):
                raw_context = value.get("context")
                raw_selection = (
                    raw_context.get("selection") if isinstance(raw_context, dict) else None
                )
                if (
                    isinstance(raw_context, dict)
                    and isinstance(raw_selection, dict)
                    and raw_selection.get("service_tier_applicability") is not None
                ):
                    try:
                        UsageServiceTierApplicability.model_validate(
                            raw_selection["service_tier_applicability"]
                        )
                    except ValueError:
                        # Unsupported proof is unknown, not permission and not
                        # a reason to discard otherwise valid native counts.
                        value = {
                            **value,
                            "context": {
                                **raw_context,
                                "selection": {
                                    **raw_selection,
                                    "service_tier_applicability": None,
                                    "captured_price_revision_id": None,
                                    "resolver_snapshot_id": None,
                                },
                            },
                        }
                        binding.request.capture.failed_events += 1
            observation = _OBSERVATION.validate_python(value)
            if not isinstance(observation, (UsageAttemptStarted, UsageAttemptFinalized)):
                binding.request.capture.failed_events += 1
                continue
            context = observation.context
            selection = context.selection
            if binding.media_operation is not None:
                if (
                    selection.operation not in {binding.media_operation, "native", "chat"}
                    or selection.provider_id != expected.selection.provider_id
                    or selection.effective_model != expected.selection.effective_model
                ):
                    binding.request.capture.failed_events += 1
                    continue
                # Frozen controller selection owns prices and routing semantics;
                # executor evidence cannot select a completion-time tariff.
                selection = expected.selection
                context = context.model_copy(update={"selection": selection})
                observation = observation.model_copy(update={"context": context})
            evidence = selection.service_tier_applicability
            candidates = (
                binding.request.executor_price_candidates.get(
                    expected.executor.inference_request_id, ()
                )
                if expected.executor is not None
                else ()
            )
            if evidence is not None or candidates:
                candidate = next(
                    (
                        candidate
                        for candidate in candidates
                        if evidence is not None
                        and evidence.state == "not_applicable"
                        and evidence.scope == candidate.scope
                        and selection.operation == candidate.scope.operation
                        and selection.operation == expected.selection.operation
                        and selection.provider_id == candidate.provider_id
                        and selection.effective_model == candidate.model
                        and selection.service_tier is None
                        and selection.effective_parameters.service_tier is None
                        and (
                            candidate.pin is None
                            or candidate.pin.resolver_snapshot_id == candidate.fingerprint
                        )
                    ),
                    None,
                )
                # The executor may report evidence, never a monetary identity.
                # Invalid proof stays captured but cannot acquire a tariff.
                selection = selection.model_copy(
                    update={
                        "service_tier_applicability": evidence if candidate is not None else None,
                        "captured_price_revision_id": candidate.pin.captured_price_revision_id
                        if candidate is not None and candidate.pin is not None
                        else None,
                        "resolver_snapshot_id": (
                            candidate.fingerprint
                            if candidate is not None and candidate.pin is not None
                            else None
                        ),
                    }
                )
                context = context.model_copy(update={"selection": selection})
                observation = observation.model_copy(update={"context": context})
            suffix = "started" if observation.kind == "attempt_started" else "finalized"
            if observation.observation_id != f"{context.trace.attempt_id}:{suffix}":
                binding.request.capture.failed_events += 1
                continue
            if observation.observation_id in binding.request.remote_event_ids:
                continue
            reductions = context.metadata_reduction.fields if context.metadata_reduction else ()
            attrs = context.attribution
            original = expected.attribution
            original_hook = original.hook
            if original_hook is not None and "hook_display_name" in reductions:
                original_hook = original_hook.model_copy(update={"hook_name": None})
            valid = (
                context.controller_owner_id == expected.controller_owner_id
                and context.coverage_epoch_id == expected.coverage_epoch_id
                and context.started_at == expected.started_at
                and context.executor == expected.executor
                and attrs.identity == original.identity
                and attrs.resources == original.resources
                and attrs.hook == original_hook
                and attrs.purpose == original.purpose
                and attrs.client_labels
                == (None if "client_labels" in reductions else original.client_labels)
                and context.trace.request_id == expected.trace.request_id
                and context.trace.cycle_id == expected.trace.cycle_id
                and context.selection.provider_id == expected.selection.provider_id
                and context.selection.effective_model == expected.selection.effective_model
            )
            previous = binding.request.previous
            if previous is not None and context.trace.attempt_id == previous.trace.attempt_id:
                seen = any(
                    event.startswith(f"{context.trace.attempt_id}:")
                    for event in binding.request.remote_event_ids
                )
                if seen:
                    valid = valid and context == previous
            if context.trace.attempt_id == expected.trace.attempt_id:
                valid = valid and context.trace == expected.trace
            elif previous is not None:
                valid = valid and (
                    context == previous
                    or (
                        context.trace.attempt_ordinal == previous.trace.attempt_ordinal + 1
                        and previous.trace.attempt_id
                        in {
                            context.trace.retry_of_attempt_id,
                            context.trace.fallback_from_attempt_id,
                        }
                    )
                )
            else:
                valid = False
            if not valid:
                binding.request.capture.failed_events += 1
                continue
            if len(binding.request.remote_event_ids) < 256:
                binding.request.remote_event_ids.add(observation.observation_id)
            binding.request.ordinal = max(binding.request.ordinal, context.trace.attempt_ordinal)
            binding.request.previous = context
            binding.request.capture.emit(observation)
        except Exception:
            binding.request.capture.failed_events += 1
    return result
