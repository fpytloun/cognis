"""Normalization for latest local-model generation performance telemetry."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from cognis.models.config import GenerationPerformanceSnapshot

_PERFORMANCE_KEYS = {
    "prompt_eval_count",
    "prompt_eval_duration",
    "eval_count",
    "eval_duration",
    "load_duration",
    "total_duration",
    "prompt_tokens_per_second",
    "generation_tokens_per_second",
    "time_to_first_token_seconds",
    "processor",
    "processors",
    "gpu",
    "gpu_residency",
    "size_vram",
}


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        data = dict(value)
    else:
        model_dump = getattr(value, "model_dump", None)
        data = model_dump(mode="json") if callable(model_dump) else {}
        if not isinstance(data, dict):
            data = {}
    hidden = getattr(value, "_hidden_params", None)
    if isinstance(hidden, dict):
        data["_hidden_params"] = hidden
    return data


def _collect_known_values(value: Any, *, depth: int = 0) -> dict[str, Any]:
    if depth > 4:
        return {}
    data = _mapping(value)
    found: dict[str, Any] = {}
    for key, item in data.items():
        if key in _PERFORMANCE_KEYS and item is not None:
            found.setdefault(key, item)
        if isinstance(item, dict | list | tuple):
            children = item.values() if isinstance(item, dict) else item
            for child in children:
                for child_key, child_value in _collect_known_values(child, depth=depth + 1).items():
                    found.setdefault(child_key, child_value)
    return found


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _ollama_duration_seconds(value: Any) -> float | None:
    number = _number(value)
    if number is None or number < 0:
        return None
    # Ollama reports durations in nanoseconds. Compatible runtimes sometimes
    # expose already-normalized seconds under the same metadata keys.
    return number / 1_000_000_000 if number >= 100_000 else number


def _display_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float | bool):
        return str(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _first_known(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _visible_delta(chunk: dict[str, Any]) -> bool:
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
            return True
    return False


class LocalGenerationPerformanceObserver:
    """Collect raw Ollama metadata before LiteLLM/JSON normalization."""

    def __init__(
        self,
        *,
        model: str,
        runtime: str,
        location: str,
        configured_context_tokens: int | None,
    ) -> None:
        self.model = model
        self.runtime = runtime
        self.location = location
        self.configured_context_tokens = configured_context_tokens
        self.started_at = monotonic()
        self.first_token_at: float | None = None
        self.values: dict[str, Any] = {}
        self.upstream: dict[str, Any] = {}
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None

    def observe_raw(self, value: Any) -> None:
        for key, item in _collect_known_values(value).items():
            self.values[key] = item

    def observe_chunk(self, chunk: dict[str, Any]) -> None:
        upstream = chunk.get("performance")
        if isinstance(upstream, dict):
            self.upstream.update(upstream)
        if self.first_token_at is None and _visible_delta(chunk):
            self.first_token_at = monotonic()
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.prompt_tokens = _integer(usage.get("prompt_tokens"))
            self.completion_tokens = _integer(usage.get("completion_tokens"))

    def snapshot(
        self,
        *,
        provider_id: str | None = None,
        provider_name: str | None = None,
        executor_id: str | None = None,
        executor_name: str | None = None,
        digest: str | None = None,
        quantization: str | None = None,
    ) -> GenerationPerformanceSnapshot:
        prompt_tokens = _first_known(
            _integer(self.values.get("prompt_eval_count")),
            self.prompt_tokens,
        )
        completion_tokens = _first_known(
            _integer(self.values.get("eval_count")),
            self.completion_tokens,
        )
        prompt_duration = _ollama_duration_seconds(self.values.get("prompt_eval_duration"))
        generation_duration = _ollama_duration_seconds(self.values.get("eval_duration"))
        prompt_rate = _number(self.values.get("prompt_tokens_per_second"))
        if prompt_rate is None and prompt_tokens is not None and prompt_duration:
            prompt_rate = prompt_tokens / prompt_duration
        generation_rate = _number(self.values.get("generation_tokens_per_second"))
        if generation_rate is None and completion_tokens is not None and generation_duration:
            generation_rate = completion_tokens / generation_duration

        upstream = self.upstream
        return GenerationPerformanceSnapshot(
            is_local=True,
            provider_id=provider_id or upstream.get("provider_id"),
            provider_name=provider_name or upstream.get("provider_name"),
            runtime=self.runtime or upstream.get("runtime"),
            location=self.location,
            executor_id=executor_id or upstream.get("executor_id"),
            executor_name=executor_name or upstream.get("executor_name"),
            model=self.model,
            digest=digest or upstream.get("digest"),
            quantization=quantization or upstream.get("quantization"),
            configured_context_tokens=_first_known(
                self.configured_context_tokens,
                _integer(upstream.get("configured_context_tokens")),
            ),
            prompt_tokens=_first_known(
                prompt_tokens,
                _integer(upstream.get("prompt_tokens")),
            ),
            completion_tokens=_first_known(
                completion_tokens,
                _integer(upstream.get("completion_tokens")),
            ),
            prompt_tokens_per_second=_first_known(
                prompt_rate,
                _number(upstream.get("prompt_tokens_per_second")),
            ),
            generation_tokens_per_second=_first_known(
                generation_rate,
                _number(upstream.get("generation_tokens_per_second")),
            ),
            # The observer closest to Cognis' caller owns TTFT. For
            # executor-routed requests this intentionally replaces the
            # executor-reported value with controller-to-first-token latency.
            time_to_first_token_seconds=_first_known(
                self.first_token_at - self.started_at if self.first_token_at is not None else None,
                _number(upstream.get("time_to_first_token_seconds")),
            ),
            load_duration_seconds=_first_known(
                _ollama_duration_seconds(self.values.get("load_duration")),
                _number(upstream.get("load_duration_seconds")),
            ),
            total_duration_seconds=_first_known(
                _ollama_duration_seconds(self.values.get("total_duration")),
                _number(upstream.get("total_duration_seconds")),
                monotonic() - self.started_at,
            ),
            processor=_display_value(
                self.values.get("processor")
                or self.values.get("processors")
                or upstream.get("processor")
            ),
            gpu_residency=_display_value(
                self.values.get("gpu_residency")
                or self.values.get("size_vram")
                or self.values.get("gpu")
                or upstream.get("gpu_residency")
            ),
            measured_at=datetime.now(UTC),
        )
