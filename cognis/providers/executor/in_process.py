"""Placeholder in-process executor provider for stage 3."""

from __future__ import annotations

import uuid

from cognis.models.config import ProviderHealth
from cognis.models.tool import ExecutorHandle


class InProcessExecutorProvider:
    """Stage-3 placeholder executor implementation."""

    async def spawn(self, labels: dict[str, str] | None = None) -> ExecutorHandle:
        return ExecutorHandle(executor_id=f"exec_{uuid.uuid4().hex[:8]}", metadata=labels)

    async def cleanup(self, executor_id: str) -> None:
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name="executor", status="healthy")
