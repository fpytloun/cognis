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

In-process / runtime invariants
--------------------------------
Some invariants cannot be checked against the database because they live
in transient in-memory state.  These are exposed as pure functions that
accept the relevant state objects and return ``InvariantResult`` instances.
They are intended for use in tests and debug builds, not in the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge
from sqlalchemy import exists, select, update
from sqlalchemy.orm import aliased

from cognis.logging import get_logger
from cognis.store.models import (
    Conversation,
    ManagedConversationLink,
    StepRun,
    Task,
    TaskDependency,
)
from cognis.store.models import Session as SessionRow

# Avoid a circular import: context_projection imports nothing from invariants.
# We import lazily inside the runtime-invariant functions below.

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
        "queued_tasks_ready_to_run",
        "Queued tasks with all required dependencies satisfied.",
    ),
    (
        "conversations_with_terminal_active_session",
        "Conversations whose active_session_id points at a terminal session.",
    ),
    (
        "conversations_with_missing_active_session",
        "Conversations whose active_session_id points at no session row.",
    ),
    (
        "managed_conversation_terminal_state",
        "Managed links with impossible terminal or restart-stale runtime state.",
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


async def reconcile_invariants(
    session: Any,
    *,
    recover_restart_stale_managed_turns: bool = False,
) -> list[InvariantReport]:
    """Repair every invariant violation in a single pass.

    Returns per-category reports with the number of rows reconciled.
    Runtime/admin reconciliation preserves live managed turns. Startup callers
    may opt into repairing queued/running turns whose in-process owner was lost
    with the previous controller process.
    """

    now = datetime.now(UTC)
    reports: list[InvariantReport] = []
    for category, description in INVARIANTS:
        reconciled = await _reconcile_category(
            session,
            category,
            now=now,
            recover_restart_stale_managed_turns=recover_restart_stale_managed_turns,
        )
        if reconciled:
            INVARIANT_RECONCILED_TOTAL.labels(category=category).inc(reconciled)
            logger.warning(
                "invariants: reconciled violations",
                extra={"extra_data": {"category": category, "count": reconciled}},
            )
        remaining = await _count_violations(
            session,
            category,
            include_restart_stale_managed_turns=recover_restart_stale_managed_turns,
        )
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


async def _count_violations(
    session: Any,
    category: str,
    *,
    include_restart_stale_managed_turns: bool = False,
) -> int:
    if category == "non_terminal_step_runs_under_terminal_task":
        return await _count_orphaned_step_runs(session)
    if category == "queued_tasks_ready_to_run":
        return await _count_queued_tasks_ready_to_run(session)
    if category == "conversations_with_terminal_active_session":
        return await _count_conversations_with_terminal_active_session(session)
    if category == "conversations_with_missing_active_session":
        return await _count_conversations_with_missing_active_session(session)
    if category == "managed_conversation_terminal_state":
        return await _count_managed_conversation_terminal_state(
            session,
            include_restart_stale=include_restart_stale_managed_turns,
        )
    return 0


async def _reconcile_category(
    session: Any,
    category: str,
    *,
    now: datetime,
    recover_restart_stale_managed_turns: bool,
) -> int:
    if category == "non_terminal_step_runs_under_terminal_task":
        return await _reconcile_orphaned_step_runs(session, now=now)
    if category == "queued_tasks_ready_to_run":
        return await _reconcile_queued_tasks_ready_to_run(session)
    if category == "conversations_with_terminal_active_session":
        return await _reconcile_conversations_with_terminal_active_session(session)
    if category == "conversations_with_missing_active_session":
        return await _reconcile_conversations_with_missing_active_session(session)
    if category == "managed_conversation_terminal_state":
        return await _reconcile_managed_conversation_terminal_state(
            session,
            now=now,
            recover_restart_stale_managed_turns=recover_restart_stale_managed_turns,
        )
    return 0


async def _count_managed_conversation_terminal_state(
    session: Any,
    *,
    include_restart_stale: bool,
) -> int:
    violation = (
        (ManagedConversationLink.conversation_state == "closed")
        & (
            (ManagedConversationLink.turn_state != "idle")
            | ManagedConversationLink.active_turn_id.is_not(None)
            | ManagedConversationLink.notify_on_completion.is_(True)
        )
    ) | (
        (ManagedConversationLink.conversation_state == "completed")
        & (
            (ManagedConversationLink.turn_state != "completed")
            | ManagedConversationLink.active_turn_id.is_not(None)
            | ManagedConversationLink.notify_on_completion.is_(True)
        )
    )
    if include_restart_stale:
        violation |= (ManagedConversationLink.conversation_state == "open") & (
            ManagedConversationLink.turn_state.in_(("queued", "running"))
        )
    stmt = select(ManagedConversationLink.link_id).where(violation)
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _reconcile_managed_conversation_terminal_state(
    session: Any,
    *,
    now: datetime,
    recover_restart_stale_managed_turns: bool,
) -> int:
    closed = await session.execute(
        update(ManagedConversationLink)
        .where(
            ManagedConversationLink.conversation_state == "closed",
            (ManagedConversationLink.turn_state != "idle")
            | ManagedConversationLink.active_turn_id.is_not(None)
            | ManagedConversationLink.notify_on_completion.is_(True),
        )
        .values(
            turn_state="idle",
            active_turn_id=None,
            notify_on_completion=False,
            updated_at=now,
        )
    )
    completed = await session.execute(
        update(ManagedConversationLink)
        .where(
            ManagedConversationLink.conversation_state == "completed",
            (ManagedConversationLink.turn_state != "completed")
            | ManagedConversationLink.active_turn_id.is_not(None)
            | ManagedConversationLink.notify_on_completion.is_(True),
        )
        .values(
            turn_state="completed",
            active_turn_id=None,
            notify_on_completion=False,
            updated_at=now,
        )
    )
    stale_count = 0
    if recover_restart_stale_managed_turns:
        stale = await session.execute(
            update(ManagedConversationLink)
            .where(
                ManagedConversationLink.conversation_state == "open",
                ManagedConversationLink.turn_state.in_(("queued", "running")),
            )
            .values(
                turn_state="interrupted",
                notify_on_completion=True,
                last_error="Controller restarted before the managed turn settled.",
                updated_at=now,
            )
        )
        stale_count = int(getattr(stale, "rowcount", 0) or 0)
    count = (
        int(getattr(closed, "rowcount", 0) or 0)
        + int(getattr(completed, "rowcount", 0) or 0)
        + stale_count
    )
    if count:
        await session.commit()
    return count


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
# Task queue invariants
# ---------------------------------------------------------------------------


def _unmet_required_dependency_exists() -> Any:
    dependency_task = aliased(Task)
    return exists(
        select(TaskDependency.task_id)
        .join(dependency_task, dependency_task.task_id == TaskDependency.depends_on)
        .where(
            TaskDependency.task_id == Task.task_id,
            TaskDependency.required.is_(True),
            dependency_task.status != "completed",
        )
    )


async def _count_queued_tasks_ready_to_run(session: Any) -> int:
    stmt = select(Task.task_id).where(
        Task.status == "queued",
        ~_unmet_required_dependency_exists(),
    )
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _reconcile_queued_tasks_ready_to_run(session: Any) -> int:
    stmt = (
        update(Task)
        .where(
            Task.status == "queued",
            ~_unmet_required_dependency_exists(),
        )
        .values(status="ready")
    )
    result = await session.execute(stmt)
    count = int(getattr(result, "rowcount", 0) or 0)
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


# ---------------------------------------------------------------------------
# Runtime (in-process) invariants — pure functions, no DB access
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InvariantResult:
    """Result of a single runtime invariant check."""

    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def check_projection_monotonicity(
    turn_state: Any,
    *,
    new_preserved_anchors: set[str],
    pressure_mode: Any,
) -> InvariantResult:
    """Verify that committed_preservations never shrinks under non-critical pressure.

    Parameters
    ----------
    turn_state:
        A ``ProjectionTurnState`` instance (typed as ``Any`` to avoid a
        circular import at module level).
    new_preserved_anchors:
        The set of group anchors that were preserved in the most recent
        ``project_messages`` call.
    pressure_mode:
        The ``PressureMode`` (or string) active for this projection.

    Returns an ``InvariantResult`` with ``passed=False`` when a non-critical
    projection would demote a previously committed group.
    """
    from cognis.core.context_projection import PressureMode  # lazy import

    committed: set[str] = getattr(turn_state, "committed_preservations", set())
    is_critical = pressure_mode == PressureMode.critical or pressure_mode == "critical"

    if is_critical:
        # Critical mode is allowed to demote anything.
        return InvariantResult(
            name="projection_monotonicity",
            passed=True,
            detail="critical mode — demotion allowed",
        )

    demoted = committed - new_preserved_anchors
    if demoted:
        return InvariantResult(
            name="projection_monotonicity",
            passed=False,
            detail=(
                f"Non-critical projection demoted {len(demoted)} previously committed "
                f"group(s): {sorted(demoted)}"
            ),
        )
    return InvariantResult(name="projection_monotonicity", passed=True)
