from __future__ import annotations

from datetime import UTC, datetime

from cognis.core.local_model_fit import FitExecutor, plan_local_model_fit
from cognis.models.executor_resources import (
    AcceleratorResourceSnapshot,
    ExecutorResourceSnapshot,
    MemoryResourceSnapshot,
)
from cognis.models.local_models import LocalModelFitMetadata, LocalModelFitStatus

GIB = 1024**3


def _executor(
    executor_id: str,
    *,
    total_gib: int,
    available_gib: int,
    unified: bool | None,
    gpu_total_gib: int | None = None,
    gpu_used_gib: int | None = None,
) -> FitExecutor:
    accelerators = (
        [
            AcceleratorResourceSnapshot(
                backend="metal" if unified else "nvidia",
                name="GPU",
                total_memory_bytes=gpu_total_gib * GIB,
                used_memory_bytes=gpu_used_gib * GIB if gpu_used_gib is not None else None,
            )
        ]
        if gpu_total_gib is not None
        else []
    )
    snapshot = ExecutorResourceSnapshot(
        observed_at=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        memory=MemoryResourceSnapshot(
            total_bytes=total_gib * GIB,
            available_bytes=available_gib * GIB,
            unified=unified,
        ),
        accelerators=accelerators,
    ).with_current_freshness(now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC))
    return FitExecutor(executor_id=executor_id, name=executor_id, snapshot=snapshot)


def _model(*, weights_gib: int = 4, advertised: int | None = 131_072) -> LocalModelFitMetadata:
    return LocalModelFitMetadata(
        requested_ref="qwen3:8b",
        weights_bytes=weights_gib * GIB,
        parameter_count=8_000_000_000,
        layer_count=32,
        kv_head_count=8,
        head_dimension=128,
        advertised_max_context=advertised,
    )


def test_exact_kv_formula_and_unified_memory_is_not_double_counted() -> None:
    result = plan_local_model_fit(
        _model(weights_gib=10),
        context_tokens=8_192,
        executors=[
            _executor(
                "unified",
                total_gib=16,
                available_gib=12,
                unified=True,
                gpu_total_gib=16,
                gpu_used_gib=1,
            )
        ],
    ).executors[0]

    expected_kv_max = 2 * 32 * 8 * 128 * 8_192 * 2
    assert result.breakdown.kv_cache_max_bytes == expected_kv_max
    assert result.admission.status == LocalModelFitStatus.NO_FIT
    assert result.admission.available_bytes == 12 * GIB
    assert result.unified_memory is True


def test_discrete_gpu_reports_offload_instead_of_averaging_pools() -> None:
    result = plan_local_model_fit(
        _model(weights_gib=8),
        context_tokens=8_192,
        executors=[
            _executor(
                "discrete",
                total_gib=32,
                available_gib=20,
                unified=False,
                gpu_total_gib=8,
                gpu_used_gib=1,
            )
        ],
    ).executors[0]

    assert result.admission.status == LocalModelFitStatus.FIT_WITH_OFFLOAD
    assert result.admission.accelerator_available_bytes == 7 * GIB
    assert result.admission.host_available_bytes == 20 * GIB - (32 * GIB * 15 // 100)
    assert result.admission.reason_codes == ["host_offload_required"]


def test_discrete_vram_fit_does_not_charge_host_headroom_to_gpu() -> None:
    result = plan_local_model_fit(
        _model(weights_gib=4),
        context_tokens=8_192,
        executors=[
            _executor(
                "discrete",
                total_gib=64,
                available_gib=40,
                unified=False,
                gpu_total_gib=8,
                gpu_used_gib=0,
            )
        ],
    ).executors[0]

    assert result.admission.status == LocalModelFitStatus.FIT
    assert result.admission.reason_codes == ["accelerator_memory_sufficient"]


def test_unknown_memory_topology_never_combines_host_and_accelerator_pools() -> None:
    result = plan_local_model_fit(
        _model(weights_gib=8),
        context_tokens=8_192,
        executors=[
            _executor(
                "unknown-topology",
                total_gib=16,
                available_gib=12,
                unified=None,
                gpu_total_gib=8,
                gpu_used_gib=1,
            )
        ],
    ).executors[0]

    assert result.static.status == LocalModelFitStatus.UNKNOWN
    assert result.admission.status == LocalModelFitStatus.UNKNOWN
    assert result.admission.reason_codes == ["memory_topology_unknown"]


def test_missing_snapshot_or_metadata_returns_unknown() -> None:
    missing_snapshot = plan_local_model_fit(
        _model(),
        context_tokens=8_192,
        executors=[FitExecutor(executor_id="offline", name="Offline", snapshot=None)],
    ).executors[0]
    missing_metadata = plan_local_model_fit(
        LocalModelFitMetadata(requested_ref="custom:latest"),
        context_tokens=8_192,
        executors=[_executor("known", total_gib=64, available_gib=60, unified=False)],
    ).executors[0]

    assert missing_snapshot.admission.status == LocalModelFitStatus.UNKNOWN
    assert missing_metadata.static.status == LocalModelFitStatus.UNKNOWN


def test_group_recommendation_uses_limiting_executor_and_caps_at_128k() -> None:
    plan = plan_local_model_fit(
        _model(weights_gib=2, advertised=262_144),
        context_tokens=32_768,
        executors=[
            _executor("large", total_gib=64, available_gib=60, unified=False),
            _executor("small", total_gib=12, available_gib=8, unified=False),
        ],
    )

    assert plan.recommended_context_tokens is not None
    assert plan.recommended_context_tokens <= 131_072
    non_green = next(option for option in plan.context_options if option.zone != "green")
    assert non_green.limiting_executor_ids == ["small"]


def test_custom_context_above_advertised_max_is_assessed_without_clamping() -> None:
    plan = plan_local_model_fit(
        _model(weights_gib=2, advertised=16_384),
        context_tokens=200_000,
        executors=[_executor("large", total_gib=128, available_gib=120, unified=False)],
    )

    assert plan.requested_context_tokens == 200_000
    assert plan.advertised_max_exceeded is True
    assert plan.executors[0].context_tokens == 200_000
    assert plan.executors[0].advertised_max_exceeded is True


def test_recommendation_never_exceeds_advertised_context() -> None:
    plan = plan_local_model_fit(
        _model(weights_gib=2, advertised=16_384),
        context_tokens=8_192,
        executors=[_executor("large", total_gib=128, available_gib=120, unified=False)],
    )

    assert plan.recommended_context_tokens == 16_384
    above_max = next(option for option in plan.context_options if option.context_tokens == 32_768)
    assert above_max.zone == "red"
    assert above_max.limiting_executor_ids == ["large"]


def test_assessment_generation_binds_exact_context() -> None:
    executor = _executor("large", total_gib=128, available_gib=120, unified=False)

    short = plan_local_model_fit(_model(), context_tokens=8_192, executors=[executor])
    long = plan_local_model_fit(_model(), context_tokens=16_384, executors=[executor])

    assert short.assessment_generation != long.assessment_generation
    assert 0 <= short.assessment_generation <= 2**53 - 1
    assert float(short.assessment_generation).is_integer()


def test_checked_arithmetic_turns_overflow_into_unknown() -> None:
    plan = plan_local_model_fit(
        _model(weights_gib=2),
        context_tokens=2**62,
        executors=[_executor("large", total_gib=128, available_gib=120, unified=False)],
    )

    assert plan.requested_context_tokens == 2**62
    assert plan.executors[0].admission.status == LocalModelFitStatus.UNKNOWN
    assert plan.executors[0].admission.reason_codes == ["model_metadata_incomplete_or_overflow"]
