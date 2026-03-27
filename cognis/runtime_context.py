"""Request-scoped runtime context using context variables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

current_user_email: ContextVar[str | None] = ContextVar("current_user_email", default=None)
current_agent_id: ContextVar[str | None] = ContextVar("current_agent_id", default=None)


@contextmanager
def scoped_runtime_context(
    *, user_email: str | None = None, agent_id: str | None = None
) -> Iterator[None]:
    """Temporarily override request-scoped runtime context variables."""

    user_token = current_user_email.set(user_email) if user_email is not None else None
    agent_token = current_agent_id.set(agent_id) if agent_id is not None else None
    try:
        yield
    finally:
        if agent_token is not None:
            current_agent_id.reset(agent_token)
        if user_token is not None:
            current_user_email.reset(user_token)
