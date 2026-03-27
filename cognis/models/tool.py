"""Domain models for tool execution and guardrails decisions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EvaluationResult(BaseModel):
    """Intaris evaluate response."""

    call_id: str
    decision: str
    reasoning: str | None = None
    risk: str | None = None
    path: str | None = None
    latency_ms: int = 0
    injection_detected: bool = False
    session_status: str | None = None
    status_reason: str | None = None


class ToolResult(BaseModel):
    """Executor or Intaris MCP tool result."""

    output: str
    is_error: bool = False
    duration_ms: int | None = None
    metadata: dict[str, Any] | None = None


class EscalationRecord(BaseModel):
    """Pending escalation record."""

    call_id: str
    session_id: str | None = None
    tool_name: str | None = None
    decision: str = "escalate"
    resolved: bool = False
    reasoning: str | None = None
    risk: str | None = None


class ExecutorHandle(BaseModel):
    """Placeholder executor handle."""

    executor_id: str
    status: str = "ready"
    metadata: dict[str, Any] | None = None
