"""Per-executor locks for token rotation and WebSocket registration."""

from __future__ import annotations

import asyncio

_LOCKS: dict[str, asyncio.Lock] = {}


def executor_token_lock(executor_id: str) -> asyncio.Lock:
    """Return the process-local lock for an executor token boundary."""
    return _LOCKS.setdefault(executor_id, asyncio.Lock())
