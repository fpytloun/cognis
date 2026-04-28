"""Task comment domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskCommentIntent(StrEnum):
    """Explicit intent attached to a human task comment."""

    RECORD_ONLY = "record_only"
    CONTEXT_ONLY = "context_only"
    REQUEST_REVISION = "request_revision"
    ANSWER_PAUSE = "answer_pause"


class TaskComment(BaseModel):
    """Human-authored task comment."""

    comment_id: str
    task_id: str
    author_email: str
    body: str
    intent: TaskCommentIntent = TaskCommentIntent.RECORD_ONLY
    noop: bool = True
    target_step: str | None = None
    confidence: float | None = None
    applied: bool = False
    attempt_number: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
