"""Request-scoped runtime context using context variables."""

from __future__ import annotations

from contextvars import ContextVar

current_user_email: ContextVar[str | None] = ContextVar("current_user_email", default=None)
current_agent_id: ContextVar[str | None] = ContextVar("current_agent_id", default=None)
