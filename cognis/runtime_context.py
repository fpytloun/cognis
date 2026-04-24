"""Request-scoped runtime context using context variables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

current_user_email: ContextVar[str | None] = ContextVar("current_user_email", default=None)
current_agent_id: ContextVar[str | None] = ContextVar("current_agent_id", default=None)
current_agent_owner_email: ContextVar[str | None] = ContextVar(
    "current_agent_owner_email", default=None
)
current_workspace_root: ContextVar[str | None] = ContextVar("current_workspace_root", default=None)
current_effective_working_directory: ContextVar[str | None] = ContextVar(
    "current_effective_working_directory", default=None
)


@contextmanager
def scoped_runtime_context(
    *,
    user_email: str | None = None,
    agent_id: str | None = None,
    agent_owner_email: str | None = None,
    workspace_root: str | None = None,
    effective_working_directory: str | None = None,
) -> Iterator[None]:
    """Temporarily override request-scoped runtime context variables."""

    user_token = current_user_email.set(user_email) if user_email is not None else None
    agent_token = current_agent_id.set(agent_id) if agent_id is not None else None
    owner_token = (
        current_agent_owner_email.set(agent_owner_email) if agent_owner_email is not None else None
    )
    workspace_token = (
        current_workspace_root.set(workspace_root) if workspace_root is not None else None
    )
    cwd_token = (
        current_effective_working_directory.set(effective_working_directory)
        if effective_working_directory is not None
        else None
    )
    try:
        yield
    finally:
        if cwd_token is not None:
            current_effective_working_directory.reset(cwd_token)
        if workspace_token is not None:
            current_workspace_root.reset(workspace_token)
        if owner_token is not None:
            current_agent_owner_email.reset(owner_token)
        if agent_token is not None:
            current_agent_id.reset(agent_token)
        if user_token is not None:
            current_user_email.reset(user_token)
