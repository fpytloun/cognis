"""Executor tool-call liveness snapshot models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE = 256
EXECUTOR_CALL_SNAPSHOT_MAX_TERMINAL = 256


class ExecutorActiveCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    started_at: datetime
    updated_at: datetime
    state: Literal["running"]


class ExecutorTerminalCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    call_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    started_at: datetime
    updated_at: datetime
    state: Literal["completed", "failed", "cancelled"]
    terminal_at: datetime


class ExecutorCallSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1]
    executor_instance_id: str = Field(min_length=1, max_length=128)
    snapshot_seq: int = Field(ge=1)
    complete: bool
    active: list[ExecutorActiveCall]
    recent_terminal: list[ExecutorTerminalCall]


def normalize_executor_call_snapshot(value: Any) -> ExecutorCallSnapshot | None:
    """Return a bounded, allowlisted call snapshot or ``None``."""

    if not isinstance(value, dict):
        return None
    active = value.get("active")
    recent_terminal = value.get("recent_terminal")
    if (
        not isinstance(active, list)
        or not isinstance(recent_terminal, list)
        or len(active) > EXECUTOR_CALL_SNAPSHOT_MAX_ACTIVE
        or len(recent_terminal) > EXECUTOR_CALL_SNAPSHOT_MAX_TERMINAL
    ):
        return None
    try:
        return ExecutorCallSnapshot.model_validate(value)
    except ValidationError:
        return None
