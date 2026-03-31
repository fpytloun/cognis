"""Task and step-run domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from cognis.models.workflow import StepEvaluation, StepOutput, WorkflowState


class TaskStatus(StrEnum):
    """Task lifecycle states."""

    DRAFT = "draft"
    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepRunStatus(StrEnum):
    """Step run lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskDelivery(BaseModel):
    """How task results are delivered to the user."""

    mode: str = "same_conversation"
    # same_conversation | specific_conversation | latest_active_for_agent
    # | preferred_channel | silent
    target: str | None = None


class TaskModel(BaseModel):
    """Durable work item visible in the kanban board."""

    task_id: str
    title: str
    description: str = ""
    expected_output: str | None = None
    status: TaskStatus = TaskStatus.DRAFT
    priority: int = 0
    created_by: str
    agent_id: str
    source_type: str = "api"  # "chat" | "api" | "scheduler" | "webhook"
    source_ref: str | None = None
    delivery: TaskDelivery = TaskDelivery()
    workflow_id: str | None = None
    workflow_state: WorkflowState | None = None
    queue_name: str = "default"
    scheduled_for: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: str | None = None
    result_data: dict[str, Any] | None = None


class StepRunModel(BaseModel):
    """A single step execution within a workflow run."""

    step_run_id: str
    task_id: str
    step_name: str
    step_type: str  # "run" | "gate"
    status: StepRunStatus = StepRunStatus.PENDING
    attempt: int = 1
    agent_id: str
    session_id: str | None = None
    intaris_session_id: str | None = None
    output: StepOutput | None = None
    evaluation: StepEvaluation | None = None
    todos: list[dict[str, Any]] = []
    started_at: datetime | None = None
    completed_at: datetime | None = None
