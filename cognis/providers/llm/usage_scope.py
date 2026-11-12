"""Execution-owned accounting metadata, never a tool authorization context."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UsageExecutionBinding:
    """Trusted attribution snapshot inherited only by related child executions."""

    actor: str
    session_id: str
    conversation_id: str
    agent_id: str
    project_id: str | None = None
    task_id: str | None = None
    step_run_id: str | None = None

    def request_values(self) -> dict[str, str]:
        """Return resource fields without fabricating absent identifiers."""
        return {
            name: value
            for name in (
                "session_id",
                "conversation_id",
                "agent_id",
                "project_id",
                "task_id",
                "step_run_id",
            )
            if (value := getattr(self, name)) is not None
        }


current_usage_execution: ContextVar[UsageExecutionBinding | None] = ContextVar(
    "usage_execution", default=None
)
