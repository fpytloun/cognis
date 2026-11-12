"""Content-free timing for stages on the interactive turn's critical path."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from cognis.logging import get_logger

logger = get_logger(__name__)
_identity: ContextVar[dict[str, str | None] | None] = ContextVar(
    "turn_timing_identity", default=None
)


def timing_identity() -> dict[str, str | None]:
    """Return identifiers only, also propagated to CPU workers."""
    return dict(_identity.get() or {})


def isolate_turn_timing[**P, T](function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Bind timing identifiers to the scheduler-owned coroutine lifetime."""

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        values: Any = kwargs
        session = values.get("session")
        token = _identity.set(
            {
                "session_id": getattr(session, "session_id", None),
                "turn_id": values.get("turn_id"),
            }
        )
        try:
            with turn_stage("turn.total"):
                return await function(*args, **kwargs)
        finally:
            _identity.reset(token)

    return wrapped


@contextmanager
def turn_stage(
    stage: str, *, session_id: str | None = None, turn_id: str | None = None
) -> Iterator[None]:
    """Log a monotonic stage duration without retaining request content."""
    started = time.perf_counter()
    outcome = "interrupted"
    try:
        yield
        outcome = "completed"
    finally:
        logger.info(
            "turn: stage completed",
            extra={
                "extra_data": {
                    **timing_identity(),
                    "stage": stage,
                    **({"session_id": session_id} if session_id is not None else {}),
                    **({"turn_id": turn_id} if turn_id is not None else {}),
                    "duration_seconds": round(time.perf_counter() - started, 6),
                    "outcome": outcome,
                }
            },
        )
