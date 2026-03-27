"""Domain models for delegation metadata."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DelegationInfo(BaseModel):
    """Child session delegation metadata."""

    mode: str
    delegated_by_session: str
    delegated_by_agent: str
    effective_agent_id: str
    task_description: str
    expected_output: str | None = None
    constraints: dict[str, Any] | None = None
