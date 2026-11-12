"""Typed transaction participants shared with canonical execution owners."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.store.feedback_models import TaskRevisionReceiptRow


@dataclass(frozen=True)
class FeedbackTaskAdmission:
    """Confirmed generation and receipt identity, not another execution owner."""

    revision_request_id: str
    submission_id: str
    request_hash: str
    expected_generation: int
    participant: Callable[[AsyncSession, TaskRevisionReceiptRow, bool], Awaitable[None]]
