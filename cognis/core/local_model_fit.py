"""Advisory local-model capacity planning from current executor snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cognis.models.executor_resources import (
    RESOURCE_SNAPSHOT_MAX_BYTES,
    ExecutorResourceSnapshot,
    normalize_executor_resource_snapshot,
)
from cognis.models.local_models import (
    LocalModelContextOption,
    LocalModelExecutorFitResult,
    LocalModelFitAssessment,
    LocalModelFitBreakdown,
    LocalModelFitConfidence,
    LocalModelFitMetadata,
    LocalModelFitPlanResponse,
    LocalModelFitStatus,
)

CONTEXT_PRESETS = (8_192, 16_384, 32_768, 65_536, 131_072, 262_144)
RECOMMENDED_CONTEXT_CAP = 131_072
RUNTIME_BUFFER_FLOOR_BYTES = 512 * 1024**2
HEADROOM_FLOOR_BYTES = 2 * 1024**3


@dataclass(slots=True)
class FitExecutor:
    """Minimal executor data consumed by the pure planner."""

    executor_id: str
    name: str
    snapshot: ExecutorResourceSnapshot | None

    @classmethod
    def from_row(cls, row: Any) -> FitExecutor:
        runtime_metadata = (
            dict(row.runtime_metadata) if isinstance(row.runtime_metadata, dict) else {}
        )
        snapshot = normalize_executor_resource_snapshot(runtime_metadata.get("resource_snapshot"))
        if snapshot is not None:
            snapshot = snapshot.with_current_freshness(
                received_at=_snapshot_received_at(runtime_metadata, row)
            )
        return cls(executor_id=str(row.executor_id), name=str(row.name), snapshot=snapshot)


@dataclass(slots=True)
class _Estimate:
    weights_bytes: int
    kv_min_bytes: int
    kv_max_bytes: int
    runtime_buffer_bytes: int
    confidence: LocalModelFitConfidence
    assumptions: list[str]


def plan_local_model_fit(
    model: LocalModelFitMetadata,
    *,
    context_tokens: int,
    executors: list[FitExecutor],
) -> LocalModelFitPlanResponse:
    """Return independent, advisory estimates and a common safe context."""

    requested_results = [
        _assess_executor(model, context_tokens=context_tokens, executor=executor)
        for executor in executors
    ]
    options: list[LocalModelContextOption] = []
    recommended: int | None = None
    preset_values = list(CONTEXT_PRESETS)
    if (
        model.advertised_max_context is not None
        and model.advertised_max_context not in preset_values
    ):
        preset_values.append(model.advertised_max_context)
        preset_values.sort()
    for preset in preset_values:
        results = [
            _assess_executor(model, context_tokens=preset, executor=executor)
            for executor in executors
        ]
        statuses = [result.admission.status for result in results]
        above_advertised = (
            model.advertised_max_context is not None and preset > model.advertised_max_context
        )
        if above_advertised:
            zone = "red"
        elif statuses and all(status == LocalModelFitStatus.FIT for status in statuses):
            zone = "green"
            if preset <= RECOMMENDED_CONTEXT_CAP:
                recommended = preset
        elif any(status == LocalModelFitStatus.NO_FIT for status in statuses):
            zone = "red"
        elif any(status == LocalModelFitStatus.UNKNOWN for status in statuses) or not statuses:
            zone = "unknown"
        else:
            zone = "yellow"
        options.append(
            LocalModelContextOption(
                context_tokens=preset,
                zone=zone,
                limiting_executor_ids=[
                    result.executor_id
                    for result in results
                    if above_advertised or result.admission.status != LocalModelFitStatus.FIT
                ],
            )
        )
    advertised_exceeded = (
        model.advertised_max_context is not None and context_tokens > model.advertised_max_context
    )
    return LocalModelFitPlanResponse(
        assessment_generation=_assessment_generation(
            model,
            context_tokens=context_tokens,
            executors=executors,
        ),
        requested_context_tokens=context_tokens,
        advertised_max_context=model.advertised_max_context,
        advertised_max_exceeded=advertised_exceeded,
        recommended_context_tokens=recommended,
        context_options=options,
        executors=requested_results,
    )


def _assess_executor(
    model: LocalModelFitMetadata,
    *,
    context_tokens: int,
    executor: FitExecutor,
) -> LocalModelExecutorFitResult:
    snapshot = executor.snapshot
    advertised_exceeded = (
        model.advertised_max_context is not None and context_tokens > model.advertised_max_context
    )
    assumptions: list[str] = []
    if advertised_exceeded:
        assumptions.append(
            "Requested context exceeds advertised metadata; the runtime may reject it."
        )
    if snapshot is None:
        unknown = _unknown_assessment("snapshot_missing")
        return LocalModelExecutorFitResult(
            executor_id=executor.executor_id,
            executor_name=executor.name,
            context_tokens=context_tokens,
            static=unknown,
            admission=unknown,
            breakdown=LocalModelFitBreakdown(),
            advertised_max_exceeded=advertised_exceeded,
            assumptions=assumptions,
        )

    estimate = _estimate_model(model, context_tokens)
    if estimate is None:
        unknown = _unknown_assessment("model_metadata_incomplete_or_overflow")
        return LocalModelExecutorFitResult(
            executor_id=executor.executor_id,
            executor_name=executor.name,
            context_tokens=context_tokens,
            static=unknown,
            admission=unknown,
            breakdown=LocalModelFitBreakdown(),
            unified_memory=snapshot.memory.unified if snapshot.memory is not None else None,
            snapshot_age_seconds=(
                snapshot.freshness.age_seconds if snapshot.freshness is not None else None
            ),
            advertised_max_exceeded=advertised_exceeded,
            assumptions=assumptions,
        )
    assumptions.extend(estimate.assumptions)

    memory = snapshot.memory
    if (
        memory is None
        or memory.total_bytes is None
        or memory.available_bytes is None
        or snapshot.accelerators is None
    ):
        unknown = _unknown_assessment("resource_memory_incomplete")
        return LocalModelExecutorFitResult(
            executor_id=executor.executor_id,
            executor_name=executor.name,
            context_tokens=context_tokens,
            static=unknown,
            admission=unknown,
            breakdown=_breakdown(estimate, None),
            unified_memory=memory.unified if memory is not None else None,
            snapshot_age_seconds=(
                snapshot.freshness.age_seconds if snapshot.freshness is not None else None
            ),
            advertised_max_exceeded=advertised_exceeded,
            assumptions=assumptions,
        )

    headroom = max(HEADROOM_FLOOR_BYTES, memory.total_bytes * 15 // 100)
    model_required_min = _checked_sum(
        estimate.weights_bytes,
        estimate.kv_min_bytes,
        estimate.runtime_buffer_bytes,
    )
    model_required_max = _checked_sum(
        estimate.weights_bytes,
        estimate.kv_max_bytes,
        estimate.runtime_buffer_bytes,
    )
    required_min = _checked_sum(
        model_required_min or RESOURCE_SNAPSHOT_MAX_BYTES + 1,
        headroom,
    )
    required_max = _checked_sum(
        model_required_max or RESOURCE_SNAPSHOT_MAX_BYTES + 1,
        headroom,
    )
    if (
        model_required_min is None
        or model_required_max is None
        or required_min is None
        or required_max is None
    ):
        unknown = _unknown_assessment("estimate_overflow")
        return LocalModelExecutorFitResult(
            executor_id=executor.executor_id,
            executor_name=executor.name,
            context_tokens=context_tokens,
            static=unknown,
            admission=unknown,
            breakdown=_breakdown(estimate, headroom),
            unified_memory=memory.unified,
            snapshot_age_seconds=(
                snapshot.freshness.age_seconds if snapshot.freshness is not None else None
            ),
            advertised_max_exceeded=advertised_exceeded,
            assumptions=assumptions,
        )

    confidence = estimate.confidence
    if snapshot.freshness is not None and snapshot.freshness.stale:
        confidence = LocalModelFitConfidence.LOW
        assumptions.append("Resource snapshot is stale; current admission may have changed.")

    if memory.unified is None and snapshot.accelerators:
        static = _unknown_assessment("memory_topology_unknown")
        admission = _unknown_assessment("memory_topology_unknown")
        assumptions.append("Host and accelerator memory may be unified; pools were not combined.")
    elif memory.unified:
        static = _single_pool_assessment(
            required_max,
            memory.total_bytes,
            confidence,
            pool_reason="unified_memory",
        )
        admission = _single_pool_assessment(
            required_max,
            memory.available_bytes,
            confidence,
            pool_reason="unified_memory",
        )
    elif snapshot.accelerators:
        totals = [accelerator.total_memory_bytes for accelerator in snapshot.accelerators]
        used = [accelerator.used_memory_bytes for accelerator in snapshot.accelerators]
        if any(value is None for value in totals):
            static = _unknown_assessment("accelerator_memory_incomplete")
        else:
            static = _discrete_assessment(
                model_required_max=model_required_max,
                accelerator_available=sum(value for value in totals if value is not None),
                host_available=memory.total_bytes,
                host_headroom=headroom,
                confidence=confidence,
            )
        if any(
            total is None or current is None for total, current in zip(totals, used, strict=True)
        ):
            admission = _unknown_assessment("accelerator_memory_incomplete")
        else:
            accelerator_free = sum(
                max(0, total - current)
                for total, current in zip(totals, used, strict=True)
                if total is not None and current is not None
            )
            admission = _discrete_assessment(
                model_required_max=model_required_max,
                accelerator_available=accelerator_free,
                host_available=memory.available_bytes,
                host_headroom=headroom,
                confidence=confidence,
            )
    else:
        static = _single_pool_assessment(
            required_max,
            memory.total_bytes,
            confidence,
            pool_reason="host_memory",
        )
        admission = _single_pool_assessment(
            required_max,
            memory.available_bytes,
            confidence,
            pool_reason="host_memory",
        )

    return LocalModelExecutorFitResult(
        executor_id=executor.executor_id,
        executor_name=executor.name,
        context_tokens=context_tokens,
        static=static,
        admission=admission,
        breakdown=LocalModelFitBreakdown(
            weights_bytes=estimate.weights_bytes,
            kv_cache_min_bytes=estimate.kv_min_bytes,
            kv_cache_max_bytes=estimate.kv_max_bytes,
            runtime_buffer_bytes=estimate.runtime_buffer_bytes,
            reserved_headroom_bytes=headroom,
            required_min_bytes=required_min,
            required_max_bytes=required_max,
        ),
        unified_memory=memory.unified,
        snapshot_age_seconds=(
            snapshot.freshness.age_seconds if snapshot.freshness is not None else None
        ),
        advertised_max_exceeded=advertised_exceeded,
        assumptions=assumptions,
    )


def _estimate_model(model: LocalModelFitMetadata, context_tokens: int) -> _Estimate | None:
    if context_tokens <= 0:
        return None
    assumptions: list[str] = []
    weights = model.weights_bytes or model.file_size_bytes
    confidence = LocalModelFitConfidence.HIGH
    if weights is None and model.parameter_count is not None and model.bits_per_weight is not None:
        weights = _checked_multiply(model.parameter_count, int(model.bits_per_weight * 1000))
        weights = (weights + 7999) // 8000 if weights is not None else None
        confidence = LocalModelFitConfidence.MEDIUM
        assumptions.append("Weights are estimated from parameter count and quantization.")
    if weights is None:
        return None

    if (
        model.layer_count is not None
        and model.kv_head_count is not None
        and model.head_dimension is not None
    ):
        kv_elements = _checked_multiply(
            2,
            model.layer_count,
            model.kv_head_count,
            model.head_dimension,
            context_tokens,
        )
        if kv_elements is None:
            return None
        kv_min = _checked_multiply(kv_elements, model.kv_bytes_per_element_min)
        kv_max = _checked_multiply(kv_elements, model.kv_bytes_per_element_max)
    elif model.parameter_count is not None:
        parameter_billions = max(1, (model.parameter_count + 999_999_999) // 1_000_000_000)
        bytes_per_token_min = _checked_multiply(parameter_billions, 8 * 1024)
        bytes_per_token_max = _checked_multiply(parameter_billions, 16 * 1024)
        kv_min = (
            _checked_multiply(bytes_per_token_min, context_tokens)
            if bytes_per_token_min is not None
            else None
        )
        kv_max = (
            _checked_multiply(bytes_per_token_max, context_tokens)
            if bytes_per_token_max is not None
            else None
        )
        confidence = LocalModelFitConfidence.MEDIUM
        assumptions.append("KV cache uses a parameter-based range because architecture is missing.")
    else:
        return None
    if kv_min is None or kv_max is None:
        return None
    runtime_buffer = _checked_sum(weights // 10, RUNTIME_BUFFER_FLOOR_BYTES)
    if runtime_buffer is None:
        return None
    return _Estimate(
        weights_bytes=weights,
        kv_min_bytes=kv_min,
        kv_max_bytes=kv_max,
        runtime_buffer_bytes=runtime_buffer,
        confidence=confidence,
        assumptions=assumptions,
    )


def _single_pool_assessment(
    required_max: int,
    available: int,
    confidence: LocalModelFitConfidence,
    *,
    pool_reason: str,
) -> LocalModelFitAssessment:
    status = LocalModelFitStatus.FIT if required_max <= available else LocalModelFitStatus.NO_FIT
    return LocalModelFitAssessment(
        status=status,
        confidence=confidence,
        available_bytes=available,
        host_available_bytes=available,
        reason_codes=[
            f"{pool_reason}_{'sufficient' if status == LocalModelFitStatus.FIT else 'insufficient'}"
        ],
    )


def _discrete_assessment(
    *,
    model_required_max: int,
    accelerator_available: int,
    host_available: int,
    host_headroom: int,
    confidence: LocalModelFitConfidence,
) -> LocalModelFitAssessment:
    usable_host = max(0, host_available - host_headroom)
    combined = _checked_sum(accelerator_available, usable_host)
    if host_available < host_headroom:
        status = LocalModelFitStatus.NO_FIT
        reason = "host_headroom_insufficient"
    elif model_required_max <= accelerator_available:
        status = LocalModelFitStatus.FIT
        reason = "accelerator_memory_sufficient"
    elif combined is not None and model_required_max <= combined:
        status = LocalModelFitStatus.FIT_WITH_OFFLOAD
        reason = "host_offload_required"
    else:
        status = LocalModelFitStatus.NO_FIT
        reason = "combined_memory_insufficient"
    return LocalModelFitAssessment(
        status=status,
        confidence=confidence,
        available_bytes=combined,
        accelerator_available_bytes=accelerator_available,
        host_available_bytes=usable_host,
        reason_codes=[reason],
    )


def _unknown_assessment(reason: str) -> LocalModelFitAssessment:
    return LocalModelFitAssessment(
        status=LocalModelFitStatus.UNKNOWN,
        confidence=LocalModelFitConfidence.LOW,
        reason_codes=[reason],
    )


def _breakdown(estimate: _Estimate, headroom: int | None) -> LocalModelFitBreakdown:
    return LocalModelFitBreakdown(
        weights_bytes=estimate.weights_bytes,
        kv_cache_min_bytes=estimate.kv_min_bytes,
        kv_cache_max_bytes=estimate.kv_max_bytes,
        runtime_buffer_bytes=estimate.runtime_buffer_bytes,
        reserved_headroom_bytes=headroom,
    )


def _checked_multiply(*values: int) -> int | None:
    result = 1
    for value in values:
        if value < 0 or result > RESOURCE_SNAPSHOT_MAX_BYTES // max(value, 1):
            return None
        result *= value
    return result


def _checked_sum(*values: int) -> int | None:
    result = 0
    for value in values:
        if value < 0 or result > RESOURCE_SNAPSHOT_MAX_BYTES - value:
            return None
        result += value
    return result


def _snapshot_received_at(runtime_metadata: dict[str, Any], row: Any) -> Any:
    value = runtime_metadata.get("resource_snapshot_received_at")
    if isinstance(value, str):
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return getattr(row, "last_observed_at", None)


def _assessment_generation(
    model: LocalModelFitMetadata,
    *,
    context_tokens: int,
    executors: list[FitExecutor],
) -> int:
    """Bind the persisted assessment marker to exact inputs and snapshots."""

    payload = {
        "model": model.model_dump(mode="json"),
        "context_tokens": context_tokens,
        "executors": [
            {
                "executor_id": executor.executor_id,
                "snapshot": (
                    executor.snapshot.model_dump(
                        mode="json",
                        exclude={"freshness"},
                    )
                    if executor.snapshot is not None
                    else None
                ),
            }
            for executor in executors
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    # Keep the persisted marker exactly round-trippable through JSON/JavaScript.
    return int.from_bytes(digest[:8], "big") & (2**53 - 1)
