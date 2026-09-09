"""Durable controller-selected runtime identity, not event-store content."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from cognis.store.models import Session


async def persist_execution_metadata(
    session_factory: Any,
    *,
    session_id: str,
    user_email: str,
    model: str,
    provider_id: str | None,
    profile_id: str | None,
    reasoning_effort: str | None,
    reasoning_mode: str | None,
    turn_id: str | None = None,
    runtime_selection_revision: int = 0,
) -> None:
    """Replace this session's runtime snapshot in a short row-locked transaction.

    The execution loop owns cancellation and serializes execution. Repeated calls
    with the same selection are idempotent. No external I/O occurs in this lock.
    The session identity prevents copied retry metadata from claiming execution.
    """
    snapshot = {
        "session_id": session_id,
        "model": model,
        "provider_id": provider_id,
        "profile_id": profile_id,
        "reasoning_effort": reasoning_effort,
        "reasoning_mode": reasoning_mode,
        "turn_id": turn_id,
        "runtime_selection_revision": runtime_selection_revision,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    async with session_factory() as db:
        row = await db.scalar(
            select(Session)
            .where(Session.session_id == session_id, Session.user_email == user_email)
            .with_for_update()
        )
        if row is None:
            raise ValueError("Execution session metadata owner was not found")
        metadata = dict(row.delegation_metadata or {})
        current = metadata.get("execution_runtime")
        comparable = dict(current) if isinstance(current, dict) else None
        if comparable is not None:
            comparable.pop("recorded_at", None)
        expected = dict(snapshot)
        expected.pop("recorded_at", None)
        if comparable != expected:
            metadata["execution_runtime"] = snapshot
            row.delegation_metadata = metadata
            await db.commit()
