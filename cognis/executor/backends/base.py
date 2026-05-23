"""Executor inference backend contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from cognis.executor.inference_types import CognisInferenceRequest


class ExecutorInferenceBackend(Protocol):
    """Transport-specific executor inference implementation.

    Backends normalize output to flattened Cognis executor chunks and must not
    execute Cognis tools; tool execution remains in the Cognis agent loop.
    """

    name: str

    def stream_complete(self, request: CognisInferenceRequest) -> AsyncIterator[dict[str, Any]]: ...

    async def generate(self, request: CognisInferenceRequest) -> dict[str, Any]: ...

    async def close(self) -> None: ...
