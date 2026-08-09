from __future__ import annotations

from time import monotonic
from typing import Any

import pytest

from cognis.api.chat_v2.realtime import runtime_overlay_from_items
from cognis.core.agent_loop import StreamAccumulator
from cognis.executor.backends.litellm import LiteLLMExecutorBackend
from cognis.executor.inference_types import CognisInferenceRequest
from cognis.providers.llm.litellm import _observe_llm_stream_request
from cognis.providers.llm.performance import LocalGenerationPerformanceObserver


class _Chunk:
    def __init__(
        self, payload: dict[str, Any], hidden_params: dict[str, Any] | None = None
    ) -> None:
        self.payload = payload
        self._hidden_params = hidden_params or {}

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.payload


async def _stream(chunks: list[_Chunk]):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_executor_normalizes_raw_ollama_performance_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        _Chunk({"choices": [{"delta": {"content": "hello"}}]}),
        _Chunk(
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            {
                "ollama": {
                    "prompt_eval_count": 100,
                    "prompt_eval_duration": 2_000_000_000,
                    "eval_count": 20,
                    "eval_duration": 1_000_000_000,
                    "load_duration": 500_000_000,
                    "total_duration": 4_000_000_000,
                    "processor": "GPU",
                    "size_vram": 8_000_000_000,
                }
            },
        ),
    ]

    async def _acompletion(**_: Any):
        return _stream(chunks)

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.acompletion", _acompletion)
    backend = LiteLLMExecutorBackend()
    output = [
        chunk
        async for chunk in backend.stream_complete(
            CognisInferenceRequest(
                model="ollama/qwen3:8b",
                messages=[{"role": "user", "content": "hi"}],
                request_kwargs={"num_ctx": 32768},
            )
        )
    ]

    performance = output[-1]["backend_metadata"]["performance"]
    assert performance["configured_context_tokens"] == 32768
    assert performance["prompt_tokens_per_second"] == pytest.approx(50)
    assert performance["generation_tokens_per_second"] == pytest.approx(20)
    assert performance["load_duration_seconds"] == pytest.approx(0.5)
    assert performance["total_duration_seconds"] == pytest.approx(4)
    assert performance["time_to_first_token_seconds"] is not None
    assert performance["processor"] == "GPU"
    assert performance["gpu_residency"] == "8000000000"


@pytest.mark.asyncio
async def test_executor_keeps_non_ollama_stream_shape_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _acompletion(**_: Any):
        return _stream(
            [
                _Chunk({"choices": [{"delta": {"content": "ok"}}]}),
                _Chunk(
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    }
                ),
            ]
        )

    monkeypatch.setattr("cognis.executor.backends.litellm.litellm.acompletion", _acompletion)
    output = [
        chunk
        async for chunk in LiteLLMExecutorBackend().stream_complete(
            CognisInferenceRequest(
                model="openai/gpt-5",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
    ]

    assert output[-1]["backend_metadata"] is None
    assert output[-1]["usage"] == {"prompt_tokens": 2, "completion_tokens": 1}


@pytest.mark.asyncio
async def test_hosted_stream_observation_exposes_usage_and_request_performance() -> None:
    telemetry: dict[str, Any] = {}

    async with _observe_llm_stream_request(
        provider_id="anthropic",
        model="claude-sonnet-4",
        llm_api="chat_completions",
        location="controller",
        telemetry=telemetry,
        started_at=monotonic() - 10,
    ) as observe_chunk:
        observe_chunk(
            {
                "choices": [{"delta": {"content": "hello"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
            }
        )

    performance = telemetry["performance"]
    assert performance["is_local"] is False
    assert performance["runtime"] == "Hosted API"
    assert performance["provider_id"] == "anthropic"
    assert performance["prompt_tokens"] == 120
    assert performance["completion_tokens"] == 30
    assert performance["time_to_first_token_seconds"] >= 10
    assert performance["total_duration_seconds"] >= 10
    assert performance["generation_tokens_per_second"] == pytest.approx(
        30 / performance["total_duration_seconds"]
    )


def test_performance_snapshot_propagates_through_stream_and_chat_runtime() -> None:
    performance = {
        "is_local": True,
        "model": "qwen3:8b",
        "runtime": "Ollama",
        "measured_at": "2026-07-13T12:00:00Z",
    }
    accumulator = StreamAccumulator()
    accumulator.feed({"choices": [], "performance": performance})

    overlay = runtime_overlay_from_items(
        conversation_id="conv-1",
        runtime_revision=1,
        has_active_turn=False,
        active_turn=None,
        volatile_items=[],
        last_generation=accumulator.performance,
    )

    assert accumulator.performance == performance
    assert overlay.last_generation is not None
    assert overlay.last_generation.model == "qwen3:8b"
    assert overlay.last_generation.runtime == "Ollama"


def test_performance_normalization_preserves_zero_and_uses_client_ttft() -> None:
    observer = LocalGenerationPerformanceObserver(
        model="qwen3:8b",
        runtime="Ollama",
        location="controller",
        configured_context_tokens=32768,
    )
    observer.observe_raw(
        {
            "prompt_eval_count": 0,
            "prompt_tokens_per_second": 0,
            "eval_count": 0,
            "generation_tokens_per_second": 0,
            "load_duration": 0,
        }
    )
    observer.observe_chunk(
        {
            "choices": [{"delta": {"content": "ready"}}],
            "performance": {"time_to_first_token_seconds": 999},
        }
    )

    snapshot = observer.snapshot()

    assert snapshot.prompt_tokens == 0
    assert snapshot.completion_tokens == 0
    assert snapshot.prompt_tokens_per_second == 0
    assert snapshot.generation_tokens_per_second == 0
    assert snapshot.load_duration_seconds == 0
    assert snapshot.time_to_first_token_seconds is not None
    assert snapshot.time_to_first_token_seconds < 999
