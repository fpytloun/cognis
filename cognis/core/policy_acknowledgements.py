"""Turn-local reuse of consecutive acknowledged Intaris configuration writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
from typing import Any


@dataclass
class _SessionAcknowledgement:
    key: str | None = None
    policy: dict[str, Any] | None = None
    generation: int = 0
    in_flight: int = 0


@dataclass
class _TurnAcknowledgements:
    active: bool = True
    sessions: dict[str, _SessionAcknowledgement] = field(default_factory=dict)


@dataclass
class PolicyAcknowledgement:
    """A cached response, or an acknowledgement supplied by a successful writer."""

    policy: dict[str, Any] | None = None

    def acknowledge(self, policy: dict[str, Any]) -> None:
        """Retain a detached successful response for the owning write."""
        self.policy = deepcopy(policy)


_acknowledgements: ContextVar[_TurnAcknowledgements | None] = ContextVar(
    "turn_policy_acknowledgements", default=None
)


def isolate_policy_acknowledgements[**P, T](
    function: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    """Own a finite acknowledgement cache for exactly one serialized turn."""

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        cache = _TurnAcknowledgements()
        token = _acknowledgements.set(cache)
        try:
            return await function(*args, **kwargs)
        finally:
            cache.active = False
            cache.sessions.clear()
            _acknowledgements.reset(token)

    return wrapped


@contextmanager
def policy_acknowledgement(
    session_id: str, request: dict[str, Any]
) -> Iterator[PolicyAcknowledgement]:
    """Reuse only the last confirmed write; failed/concurrent writes invalidate it."""
    cache = _acknowledgements.get()
    ticket = PolicyAcknowledgement()
    if cache is None or not cache.active:
        yield ticket
        return
    if session_id not in cache.sessions and len(cache.sessions) >= 16:
        removable = next(
            (key for key, value in cache.sessions.items() if not value.in_flight), None
        )
        if removable is None:
            yield ticket
            return
        del cache.sessions[removable]
    state = cache.sessions.setdefault(session_id, _SessionAcknowledgement())
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if not state.in_flight and key == state.key and state.policy is not None:
        yield PolicyAcknowledgement(deepcopy(state.policy))
        return
    # A -> B -> A must write A again, even if B failed after delivery.
    state.key = None
    state.policy = None
    state.generation += 1
    generation = state.generation
    exclusive = state.in_flight == 0
    state.in_flight += 1
    completed = False
    try:
        yield ticket
        completed = True
    finally:
        state.in_flight -= 1
        if (
            cache.active
            and completed
            and exclusive
            and state.generation == generation
            and not state.in_flight
            and ticket.policy is not None
        ):
            state.key = key
            state.policy = deepcopy(ticket.policy)
