"""Durable same-executor recovery windows for active work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

EXECUTOR_RECOVERY_WINDOW_SECONDS = 900


@dataclass(frozen=True, slots=True)
class ExecutorRecoveryWindow:
    """A recovery window anchored at the first unavailable observation."""

    executor_id: str
    unavailable_since: datetime
    deadline: datetime
    database_remaining_seconds: float

    def remaining_seconds(self, *, now: datetime | None = None) -> float:
        if now is None:
            return self.database_remaining_seconds
        return max(0.0, (self.deadline - now).total_seconds())

    def detail(self, *, phase: str) -> dict[str, Any]:
        return {
            "code": "executor_recovery_timeout",
            "executor_id": self.executor_id,
            "unavailable_since": self.unavailable_since.isoformat(),
            "deadline": self.deadline.isoformat(),
            "waited_seconds": EXECUTOR_RECOVERY_WINDOW_SECONDS,
            "phase": phase,
        }


class ExecutorRecoveryTimeout(RuntimeError):
    """The selected executor did not become ready before its durable deadline."""

    def __init__(self, window: ExecutorRecoveryWindow, *, phase: str) -> None:
        self.window = window
        self.phase = phase
        self.detail = window.detail(phase=phase)
        super().__init__(
            f"Selected executor '{window.executor_id}' did not become ready within "
            f"{EXECUTOR_RECOVERY_WINDOW_SECONDS} seconds."
        )


async def begin_executor_recovery(
    session_factory: Any,
    *,
    conversation_id: str | None,
    task_id: str | None,
    executor_id: str,
    observed_at: datetime | None = None,
) -> ExecutorRecoveryWindow:
    """Persist and return the first unavailable observation for active work."""

    from cognis.store.queries import get_conversation, get_task, mark_executor_unavailable

    async with session_factory() as session:
        from cognis.store.coordination import database_now

        database_current = await database_now(session)
        row = (
            await get_task(session, task_id)
            if task_id
            else await get_conversation(session, conversation_id)
            if conversation_id
            else None
        )
        unavailable_since = getattr(row, "active_executor_unavailable_since", None)
        if (
            row is not None
            and getattr(row, "active_executor_id", None) == executor_id
            and unavailable_since is None
        ):
            _, persisted = await mark_executor_unavailable(
                session,
                conversation_id=conversation_id,
                task_id=task_id,
                expected_executor_id=executor_id,
                expected_generation=int(getattr(row, "active_executor_generation", 0) or 0),
                observed_at=observed_at,
            )
            unavailable_since = persisted or database_current
            await session.commit()
        if unavailable_since is None:
            unavailable_since = database_current
    if unavailable_since.tzinfo is None:
        unavailable_since = unavailable_since.replace(tzinfo=UTC)
    deadline = unavailable_since + timedelta(seconds=EXECUTOR_RECOVERY_WINDOW_SECONDS)
    return ExecutorRecoveryWindow(
        executor_id=executor_id,
        unavailable_since=unavailable_since,
        deadline=deadline,
        database_remaining_seconds=max(0.0, (deadline - database_current).total_seconds()),
    )


async def clear_executor_recovery(
    session_factory: Any,
    *,
    conversation_id: str | None,
    task_id: str | None,
    executor_id: str,
) -> None:
    """Clear the outage marker after the same executor becomes ready."""

    from cognis.store.queries import clear_executor_unavailable, get_conversation, get_task

    async with session_factory() as session:
        row = (
            await get_task(session, task_id)
            if task_id
            else await get_conversation(session, conversation_id)
            if conversation_id
            else None
        )
        if row is None or getattr(row, "active_executor_id", None) != executor_id:
            return
        await clear_executor_unavailable(
            session,
            conversation_id=conversation_id,
            task_id=task_id,
            expected_executor_id=executor_id,
            expected_generation=int(getattr(row, "active_executor_generation", 0) or 0),
        )
        await session.commit()
