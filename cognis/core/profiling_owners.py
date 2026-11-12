"""Explicit composition of fixed-label, content-free owner observations."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from cognis.core.resource_attribution import MemorySummary


class ProfilingOwners:
    """Read session estimates and O(1) owner cardinalities only when requested.

    Providers must return fixed labels and integer counts, never runtime keys,
    payloads, serialized objects or graph references. Counts are not byte sizes.
    """

    def __init__(
        self,
        sessions: MemorySummary,
        counts: Mapping[str, Callable[[], dict[str, int]] | None],
    ) -> None:
        if len(counts) > 8:
            raise ValueError("too_many_profiling_owners")
        self.sessions = sessions
        self.counts = dict(counts)

    async def __call__(self) -> dict[str, Any]:
        """Collect detached counters without hydrating or mutating any owner."""
        counters: dict[str, Any] = {}
        for label, provider in self.counts.items():
            if provider is None:
                counters[label] = {"available": False}
                continue
            try:
                observed = provider()
                if len(observed) > 64 or any(
                    type(value) is not int or value < 0 for value in observed.values()
                ):
                    raise ValueError("invalid_owner_counts")
                counters[label] = {
                    "available": True,
                    "kind": "cardinality_not_bytes",
                    "counts": dict(observed),
                }
            except Exception:
                counters[label] = {"available": False, "error": "owner_counts_failed"}
        result = self.sessions()
        if inspect.isawaitable(result):
            result = await result
        return {**result, "owner_counts": counters}
