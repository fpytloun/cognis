"""Controller-owned, per-dispatch program runtime policy resolution."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from cognis.core.program_config import program_runtime_executor_enabled
from cognis.store.queries import get_executor_row


class ProgramPolicyResolver:
    """Resolve the actual connection's configured identity in the owner's scope."""

    def __init__(
        self, session_factory: Any, owner_email: str, bindings: Mapping[str, str] | None = None
    ) -> None:
        self._session_factory = session_factory
        self._owner_email = owner_email
        self._bindings = MappingProxyType(dict(bindings or {}))

    async def __call__(self, connection: Any) -> bool:
        runtime_id = getattr(connection, "executor_id", None)
        if not isinstance(runtime_id, str) or not runtime_id or not self._owner_email:
            return False
        configured_id = self._bindings.get(runtime_id, runtime_id)
        try:
            async with self._session_factory() as session:
                row = await get_executor_row(
                    session,
                    configured_id,
                    owner_email=self._owner_email,
                    include_shared=True,
                    defer_observed_tools=True,
                )
                if row is None:
                    return False
                return program_runtime_executor_enabled(
                    {"executor_id": row.executor_id, "labels": row.labels, "config": row.config}
                )
        except Exception:
            return False
