"""Deliverable domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, field_validator


class DeliverableFormat(StrEnum):
    """Supported deliverable render formats."""

    MARKDOWN = "markdown"
    PLAIN = "plain"
    HTML = "html"


class DeliverableStatus(StrEnum):
    """Lifecycle states for a step deliverable."""

    BUFFERED = "buffered"
    APPROVED = "approved"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Deliverable(BaseModel):
    """Typed, versioned artifact authored by a workflow step."""

    deliverable_id: str
    step_run_id: str
    version: int
    attempt_number: int = 1
    content: str
    format: Literal["markdown", "plain", "html"] = DeliverableFormat.MARKDOWN
    title: str | None = None
    target: Literal["channel", "none"] | None = None
    outputs: dict[str, Any] = {}
    status: DeliverableStatus = DeliverableStatus.BUFFERED
    evaluator_feedback: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be empty")
        return value
