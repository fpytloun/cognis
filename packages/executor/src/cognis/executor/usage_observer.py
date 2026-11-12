"""Request-local executor observations, piggybacked on existing RPC messages."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from cognis.models.usage import (
    USAGE_OBSERVATION_MAX_BYTES,
    UsageAttemptContext,
    UsageAttribution,
    UsageExecutorContext,
    UsageMediaOperation,
    UsageModelSelection,
    UsageObservation,
)
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_capture import UsageAttempt, UsageCapture, UsageRequest

CONTEXT_KEY = "cognis_usage_context"
OBSERVATIONS_KEY = "cognis_usage_observations"


def _wire_observation(observation: UsageObservation) -> dict[str, Any]:
    # The union discriminator is a model default, but is mandatory on the wire.
    return observation.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    ) | {"kind": observation.kind}


class ExecutorUsageCancelled(asyncio.CancelledError):
    """Keep task cancellation semantics while returning bounded terminal metadata."""

    def __init__(self, result: dict[str, Any]):
        super().__init__()
        self.result = result


class ExecutorUsageRequest(UsageRequest):
    """Use controller correlation, not a grant or an outer RPC ledger."""

    def __init__(self, capture: UsageCapture, seed: UsageAttemptContext):
        super().__init__(
            capture=capture,
            attribution=seed.attribution,
            request_id=seed.trace.request_id,
            cycle_id=seed.trace.cycle_id,
            requested_model=seed.selection.requested_model,
            requested_parameters=seed.selection.requested_parameters,
        )
        self.seed = seed
        self.ordinal = seed.trace.attempt_ordinal - 1
        self.first_attempt = True

    def begin(
        self,
        *,
        selection: UsageModelSelection,
        executor: UsageExecutorContext | None = None,
        clock_domain: Literal["controller", "executor"] = "executor",
        semantic_profile: Literal["openai", "anthropic", "unknown"] = "unknown",
        emit_start: bool = True,
        attribution: UsageAttribution | None = None,
    ) -> UsageAttempt:
        if self.first_attempt:
            self.first_attempt = False
            self.ordinal = self.seed.trace.attempt_ordinal
            context = self.seed.model_copy(update={"selection": selection})
        else:
            context = (
                super()
                .begin(
                    selection=selection,
                    executor=self.seed.executor,
                    clock_domain="executor",
                    semantic_profile=semantic_profile,
                    emit_start=False,
                )
                .context
            )
            # The controller timestamp is the immutable trusted retention anchor.
            # Actual dispatch/generation durations use executor-local clocks.
            context = context.model_copy(update={"started_at": self.seed.started_at})
        context = self._fit_context(context)
        self.previous = context
        return UsageAttempt(
            capture=self.capture,
            context=context,
            clock_domain="executor",
            semantic_profile=semantic_profile,
            emit_start=emit_start,
        )


class ExecutorUsageObserver:
    """Bounded byte/item buffer; physical adapters alone own the attempts."""

    def __init__(
        self, context: UsageAttemptContext, media_operation: UsageMediaOperation | None = None
    ) -> None:
        self.events: list[UsageObservation] = []
        self.pending_bytes = 0
        self.dropped = 0
        self.media_accounting = media_operation is not None
        self.capture = UsageCapture(
            sink=self,
            controller_owner_id=context.controller_owner_id,
            coverage_epoch_id=context.coverage_epoch_id,
        )
        self.request = ExecutorUsageRequest(self.capture, context)
        self.binding = UsageBinding(
            self.request,
            context.selection.provider_id,
            context.selection.effective_model,
            media_operation=media_operation,
        )

    def try_emit(self, observation: UsageObservation) -> bool:
        """Acceptance means memory only, never RPC delivery or SQL durability."""
        size = len(
            json.dumps(
                _wire_observation(observation),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        if (
            size > USAGE_OBSERVATION_MAX_BYTES
            or len(self.events) >= 32
            or self.pending_bytes + size > 64 * 1024
        ):
            self.dropped += 1
            return False
        self.events.append(observation)
        self.pending_bytes += size
        return True

    def attach(self, result: dict[str, Any]) -> dict[str, Any]:
        """Drain only into a response/chunk already being sent, never extra I/O."""
        if not self.events:
            return result
        metadata = dict(result.get("backend_metadata") or {})
        metadata[OBSERVATIONS_KEY] = [_wire_observation(event) for event in self.events]
        if not self.media_accounting:
            # Old controllers did not opt into the additive media dimensions.
            for value in metadata[OBSERVATIONS_KEY]:
                selection = value["context"]["selection"]
                for key in ("request_parameters", "effective_parameters"):
                    parameters = selection.get(key, {})
                    parameters.pop("image_size", None)
                    parameters.pop("image_quality", None)
                    if "provenance" in parameters:
                        parameters["provenance"] = [
                            item
                            for item in parameters["provenance"]
                            if item["parameter"] not in {"image_size", "image_quality"}
                        ]
                usage = value.get("usage")
                if usage:
                    usage.pop("provider_cost", None)
                    usage["extras"] = [
                        {**extra, "status": "reported"}
                        if extra.get("status") == "derived"
                        else extra
                        for extra in usage.get("extras", [])
                        if extra["name"]
                        not in {
                            "output_image_tokens",
                            "input_text_tokens",
                            "output_text_tokens",
                            "cache_read_image_tokens",
                            "cache_read_audio_tokens",
                        }
                    ]
        self.events.clear()
        self.pending_bytes = 0
        return {**result, "backend_metadata": metadata}


def executor_observer(metadata: dict[str, Any] | None) -> ExecutorUsageObserver | None:
    """Malformed telemetry is a diagnosed coverage gap, not an inference gate."""
    try:
        raw = (metadata or {}).get(CONTEXT_KEY)
        operation = (metadata or {}).get("cognis_media_operation")
        if (metadata or {}).get("cognis_media_usage_version") != 1 or operation not in {
            "image_generate",
            "image_edit",
            "transcribe",
            "synthesize",
            "video_generate",
            "video_extend",
        }:
            operation = None
        return (
            ExecutorUsageObserver(UsageAttemptContext.model_validate(raw), operation)
            if raw
            else None
        )
    except Exception:
        return None
