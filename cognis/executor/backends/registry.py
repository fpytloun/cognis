"""Registry for executor-local inference transports."""

from __future__ import annotations

from collections.abc import Callable

from cognis.executor.backends.base import ExecutorInferenceBackend
from cognis.executor.backends.litellm import LiteLLMExecutorBackend
from cognis.executor.inference_types import CognisInferenceRequest

BackendFactory = Callable[[], ExecutorInferenceBackend]


class ExecutorBackendRegistry:
    """Instantiate and cache executor inference backends by name."""

    def __init__(self) -> None:
        self._factories: dict[str, BackendFactory] = {
            "litellm": LiteLLMExecutorBackend,
        }
        self._instances: dict[str, ExecutorInferenceBackend] = {}

    def register(self, name: str, factory: BackendFactory) -> None:
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("Executor backend name cannot be empty")
        self._factories[normalized] = factory

    def get(self, name: str) -> ExecutorInferenceBackend:
        normalized = (name or "litellm").strip().lower()
        factory = self._factories.get(normalized)
        if factory is None:
            raise ValueError(f"Unknown executor inference backend: {normalized}")
        if normalized not in self._instances:
            self._instances[normalized] = factory()
        return self._instances[normalized]

    def select(self, request: CognisInferenceRequest) -> ExecutorInferenceBackend:
        return self.get(request.backend)

    async def close(self) -> None:
        for backend in self._instances.values():
            await backend.close()
        self._instances.clear()


def resolve_backend_name(request_kwargs: dict[str, object], explicit_backend: str | None) -> str:
    """Resolve backend without treating provider-native LiteLLM kwargs as Cognis routing."""

    if explicit_backend:
        return explicit_backend
    candidate = request_kwargs.pop("cognis_inference_backend", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    return "litellm"
