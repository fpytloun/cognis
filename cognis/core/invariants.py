"""Cross-subsystem invariant checkers and reconcilers.

Stage 20+ refactors introduced several lifecycle transitions that could
leak persistent state when failure paths did not finalize dependent
rows. This module codifies the invariants in one place so every
violation has:

* a read-only checker that returns a count (useful as a runtime gauge
  and for the admin ``/system/invariants`` endpoint);
* an idempotent reconciler that repairs the violation (useful on
  startup and as an on-demand admin command).

Adding a new invariant here means one place to document the contract,
run it in tests, and observe it in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge
from sqlalchemy import select, update

from cognis.logging import get_logger
from cognis.store.models import Conversation, StepRun, Task
from cognis.store.models import Session as SessionRow

logger = get_logger(__name__)

_TASK_TERMINAL_STATES = ("failed", "completed", "cancelled")
_STEPRUN_NON_TERMINAL_STATES = ("running", "paused", "evaluating", "pending")
_SESSION_TERMINAL_STATES = ("completed", "failed", "cancelled", "terminated")


INVARIANT_RECONCILED_TOTAL = Counter(
    "cognis_invariant_reconciled_total",
    "Number of invariant violations repaired on startup or on demand.",
    labelnames=("category",),
)
INVARIANT_CURRENT_GAUGE = Gauge(
    "cognis_invariant_current",
    "Current number of invariant violations (read-only probe).",
    labelnames=("category",),
)


@dataclass(slots=True)
class InvariantReport:
    """Summary for a single invariant category."""

    category: str
    description: str
    current_count: int = 0
    reconciled_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "description": self.description,
            "current_count": self.current_count,
            "reconciled_count": self.reconciled_count,
        }


INVARIANTS: tuple[tuple[str, str], ...] = (
    (
        "non_terminal_step_runs_under_terminal_task",
        "StepRun rows in non-terminal status whose parent task is terminal.",
    ),
    (
        "conversations_with_terminal_active_session",
        "Conversations whose active_session_id points at a terminal session.",
    ),
    (
        "conversations_with_missing_active_session",
        "Conversations whose active_session_id points at no session row.",
    ),
)


async def check_invariants(session: Any) -> list[InvariantReport]:
    """Return read-only counts for every invariant category."""

    reports: list[InvariantReport] = []
    for category, description in INVARIANTS:
        count = await _count_violations(session, category)
        reports.append(
            InvariantReport(
                category=category,
                description=description,
                current_count=count,
            )
        )
        INVARIANT_CURRENT_GAUGE.labels(category=category).set(count)
    return reports


async def reconcile_invariants(session: Any) -> list[InvariantReport]:
    """Repair every invariant violation in a single pass.

    Returns per-category reports with the number of rows reconciled.
    Intended for startup and for the admin reconcile command; the
    implementation is idempotent, so repeated runs converge to zero.
    """

    now = datetime.now(UTC)
    reports: list[InvariantReport] = []
    for category, description in INVARIANTS:
        reconciled = await _reconcile_category(session, category, now=now)
        if reconciled:
            INVARIANT_RECONCILED_TOTAL.labels(category=category).inc(reconciled)
            logger.warning(
                "invariants: reconciled violations",
                extra={"extra_data": {"category": category, "count": reconciled}},
            )
        remaining = await _count_violations(session, category)
        INVARIANT_CURRENT_GAUGE.labels(category=category).set(remaining)
        reports.append(
            InvariantReport(
                category=category,
                description=description,
                current_count=remaining,
                reconciled_count=reconciled,
            )
        )
    return reports


async def _count_violations(session: Any, category: str) -> int:
    if category == "non_terminal_step_runs_under_terminal_task":
        return await _count_orphaned_step_runs(session)
    if category == "conversations_with_terminal_active_session":
        return await _count_conversations_with_terminal_active_session(session)
    if category == "conversations_with_missing_active_session":
        return await _count_conversations_with_missing_active_session(session)
    return 0


async def _reconcile_category(session: Any, category: str, *, now: datetime) -> int:
    if category == "non_terminal_step_runs_under_terminal_task":
        return await _reconcile_orphaned_step_runs(session, now=now)
    if category == "conversations_with_terminal_active_session":
        return await _reconcile_conversations_with_terminal_active_session(session)
    if category == "conversations_with_missing_active_session":
        return await _reconcile_conversations_with_missing_active_session(session)
    return 0


# ---------------------------------------------------------------------------
# StepRun invariants
# ---------------------------------------------------------------------------


async def _count_orphaned_step_runs(session: Any) -> int:
    stmt = select(StepRun.step_run_id).where(
        StepRun.status.in_(_STEPRUN_NON_TERMINAL_STATES),
        StepRun.task_id.in_(select(Task.task_id).where(Task.status.in_(_TASK_TERMINAL_STATES))),
    )
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _reconcile_orphaned_step_runs(session: Any, *, now: datetime) -> int:
    from cognis.store.queries import fail_orphaned_running_step_runs

    count = await fail_orphaned_running_step_runs(session, now)
    if count:
        await session.commit()
    return count


# ---------------------------------------------------------------------------
# Conversation invariants
# ---------------------------------------------------------------------------


async def _count_conversations_with_terminal_active_session(session: Any) -> int:
    terminal_session_ids = select(SessionRow.session_id).where(
        SessionRow.status.in_(_SESSION_TERMINAL_STATES)
    )
    stmt = select(Conversation.conversation_id).where(
        Conversation.active_session_id.is_not(None),
        Conversation.active_session_id.in_(terminal_session_ids),
    )
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _reconcile_conversations_with_terminal_active_session(session: Any) -> int:
    terminal_session_ids = select(SessionRow.session_id).where(
        SessionRow.status.in_(_SESSION_TERMINAL_STATES)
    )
    stmt = (
        update(Conversation)
        .where(
            Conversation.active_session_id.is_not(None),
            Conversation.active_session_id.in_(terminal_session_ids),
        )
        .values(active_session_id=None)
    )
    result = await session.execute(stmt)
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        await session.commit()
    return count


async def _count_conversations_with_missing_active_session(session: Any) -> int:
    existing_session_ids = select(SessionRow.session_id)
    stmt = select(Conversation.conversation_id).where(
        Conversation.active_session_id.is_not(None),
        Conversation.active_session_id.not_in(existing_session_ids),
    )
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _reconcile_conversations_with_missing_active_session(session: Any) -> int:
    existing_session_ids = select(SessionRow.session_id)
    stmt = (
        update(Conversation)
        .where(
            Conversation.active_session_id.is_not(None),
            Conversation.active_session_id.not_in(existing_session_ids),
        )
        .values(active_session_id=None)
    )
    result = await session.execute(stmt)
    count = int(getattr(result, "rowcount", 0) or 0)
    if count:
        await session.commit()
    return count
