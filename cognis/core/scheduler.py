"""Schedule evaluator — creates Tasks when schedules fire.

The Scheduler is a task factory that runs a timer loop, evaluates
cron/interval/one-shot schedules, and creates Tasks via TaskQueue.submit()
when they become due.  It is started alongside the TaskQueue during app
startup and stopped before it during shutdown.

Schedules can set an explicit completion delivery family (default or direct)
and optionally allow silent completion when the agent finishes with nothing
user-actionable to report.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.task import TaskDelivery
from cognis.models.workflow import CompletionDeliveryPolicy
from cognis.store.queries import (
    count_active_tasks_for_schedule,
    delete_schedule,
    get_schedule,
    list_due_schedules,
    list_schedules,
    update_schedule,
    update_schedule_fire_state,
)

logger = get_logger(__name__)

# Backoff schedule for consecutive errors (seconds).
_BACKOFF_SCHEDULE = [30, 60, 300, 900, 3600]

# Maximum sleep between timer ticks (seconds).  Prevents drift from
# long sleeps when the next fire is far in the future.
_MAX_SLEEP_SECONDS = 60

# Default settings (overridden by DB settings).
_DEFAULT_MAX_MISSED_ON_STARTUP = 5
_DEFAULT_MISSED_STAGGER_SECONDS = 5
_DEFAULT_MAX_CONSECUTIVE_ERRORS = 5


class Scheduler:
    """Cron/interval/one-shot schedule evaluator.

    Creates Tasks via ``TaskQueue.submit()`` when schedules become due.
    """

    def __init__(
        self,
        session_factory: Any,
        task_queue: Any,
        event_bus: EventBus,
        *,
        max_missed_on_startup: int = _DEFAULT_MAX_MISSED_ON_STARTUP,
        missed_stagger_seconds: int = _DEFAULT_MISSED_STAGGER_SECONDS,
        max_consecutive_errors: int = _DEFAULT_MAX_CONSECUTIVE_ERRORS,
    ) -> None:
        self._session_factory = session_factory
        self._task_queue = task_queue
        self._event_bus = event_bus
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._timer_task: asyncio.Task[None] | None = None
        self._running = False

        # Configurable limits
        self._max_missed_on_startup = max_missed_on_startup
        self._missed_stagger_seconds = missed_stagger_seconds
        self._max_consecutive_errors = max_consecutive_errors

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise next_fire_at for all schedules, catch up missed, start timer."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        try:
            await self._initialise_next_fire_times()
            await self._catch_up_missed()
        except Exception:
            # Gracefully handle missing columns (migration not yet applied).
            # The scheduler will still start and work once the migration runs.
            logger.warning(
                "Scheduler startup initialisation failed (migration pending?); "
                "continuing with empty schedule cache",
                exc_info=True,
            )
        self._timer_task = asyncio.create_task(self._timer_loop(), name="scheduler-timer")
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Signal the timer loop to stop and wait for it to finish."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        self._wake_event.set()  # unblock any sleep
        if self._timer_task is not None:
            try:
                await asyncio.wait_for(self._timer_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._timer_task.cancel()
            self._timer_task = None
        logger.info("Scheduler stopped")

    def wake(self) -> None:
        """Signal the timer loop to re-evaluate immediately."""
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Timer loop
    # ------------------------------------------------------------------

    async def _timer_loop(self) -> None:
        """Main loop: sleep until next due schedule, fire, repeat."""
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("Scheduler tick error")
                # Avoid tight error loop
                await self._interruptible_sleep(5.0)

    async def _tick(self) -> None:
        """Single timer tick: fire due schedules, then sleep."""
        now = datetime.now(UTC)

        async with self._db_session() as db:
            due = await list_due_schedules(db, now)

        for sched_row in due:
            if self._stop_event.is_set():
                break
            await self._fire_schedule(sched_row.schedule_id)

        # Compute sleep duration
        sleep_seconds = await self._seconds_until_next()
        sleep_seconds = min(sleep_seconds, _MAX_SLEEP_SECONDS)
        sleep_seconds = max(sleep_seconds, 1.0)

        await self._interruptible_sleep(sleep_seconds)

    async def _seconds_until_next(self) -> float:
        """Return seconds until the next enabled schedule fires."""
        async with self._db_session() as db:
            from sqlalchemy import select as sa_select

            from cognis.store.models import Schedule

            stmt = (
                sa_select(Schedule.next_fire_at)
                .where(Schedule.enabled.is_(True), Schedule.next_fire_at.isnot(None))
                .order_by(Schedule.next_fire_at)
                .limit(1)
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()

        if row is None:
            return _MAX_SLEEP_SECONDS

        delta = (row - datetime.now(UTC)).total_seconds()
        return max(delta, 0.0)

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for *seconds*, but wake early if signalled or stopped."""
        self._wake_event.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)

    # ------------------------------------------------------------------
    # Fire logic
    # ------------------------------------------------------------------

    async def _fire_schedule(self, schedule_id: str) -> None:
        """Create a task from the schedule template."""
        now = datetime.now(UTC)

        async with self._db_session() as db:
            sched = await get_schedule(db, schedule_id)
            if sched is None or not sched.enabled:
                return

            # Check concurrency limit
            active_count = await count_active_tasks_for_schedule(db, schedule_id)
            if active_count >= sched.max_concurrent_runs:
                logger.info(
                    "Schedule %s skipped: %d/%d active tasks",
                    schedule_id,
                    active_count,
                    sched.max_concurrent_runs,
                )
                next_fire = self._compute_next_fire(sched, now)
                await update_schedule_fire_state(
                    db,
                    schedule_id,
                    last_fired_at=now,
                    next_fire_at=next_fire,
                    last_run_status="skipped",
                    consecutive_errors=sched.consecutive_errors,
                )
                await db.commit()
                return

        # Create the task (outside the schedule DB session)
        try:
            template: dict[str, Any] = dict(sched.task_template)
            title = template.pop("title", sched.name)
            description = template.pop("description", sched.description or "")
            expected_output = template.pop("expected_output", None)
            priority = template.pop("priority", 0)
            workflow_id = template.pop("workflow_id", None) or sched.workflow_id
            workspace_root = template.pop("workspace_root", None)
            working_directory = template.pop("working_directory", None)

            # Delivery config from template
            delivery_raw = template.pop("delivery", None)
            delivery: TaskDelivery | None = None
            if isinstance(delivery_raw, dict):
                delivery = TaskDelivery(**delivery_raw)
            completion_delivery = CompletionDeliveryPolicy(
                completion_mode_family=getattr(sched, "completion_mode_family", "default"),
                allow_silent_completion=bool(getattr(sched, "allow_silent_completion", False)),
            )

            task = await self._task_queue.submit(
                created_by=sched.created_by,
                agent_id=sched.agent_id,
                title=str(title),
                description=str(description),
                expected_output=expected_output,
                priority=int(priority),
                source_type="scheduler",
                source_ref=schedule_id,
                delivery=delivery,
                completion_delivery=completion_delivery,
                workflow_id=workflow_id,
                workspace_root=workspace_root,
                working_directory=working_directory,
            )

            logger.info(
                "Schedule %s fired -> task %s",
                schedule_id,
                task.task_id,
            )

            # Update fire state — success
            next_fire = self._compute_next_fire(sched, now)
            async with self._db_session() as db:
                await update_schedule_fire_state(
                    db,
                    schedule_id,
                    last_fired_at=now,
                    next_fire_at=next_fire,
                    last_run_status="success",
                    consecutive_errors=0,
                )
                # Auto-delete one-shot schedules
                if sched.delete_after_run and sched.schedule_type == "one_shot":
                    await delete_schedule(db, schedule_id)
                    logger.info("One-shot schedule %s deleted after fire", schedule_id)
                await db.commit()

            await self._event_bus.publish(
                Event(
                    type=EventType.SCHEDULE_FIRED,
                    data={
                        "schedule_id": schedule_id,
                        "task_id": task.task_id,
                    },
                )
            )

        except Exception:
            logger.exception("Schedule %s fire failed", schedule_id)
            errors = sched.consecutive_errors + 1
            disabled = errors >= self._max_consecutive_errors
            backoff = self._compute_backoff_delay(errors)
            next_fire = now + timedelta(seconds=backoff)

            async with self._db_session() as db:
                await update_schedule_fire_state(
                    db,
                    schedule_id,
                    last_fired_at=now,
                    next_fire_at=next_fire if not disabled else None,
                    last_run_status="failed",
                    consecutive_errors=errors,
                    disabled_reason=(
                        f"Auto-disabled after {errors} consecutive errors" if disabled else None
                    ),
                    enabled=False if disabled else None,
                )
                await db.commit()

            if disabled:
                logger.warning(
                    "Schedule %s auto-disabled after %d consecutive errors",
                    schedule_id,
                    errors,
                )
                await self._event_bus.publish(
                    Event(
                        type=EventType.SCHEDULE_DISABLED,
                        data={
                            "schedule_id": schedule_id,
                            "reason": f"Auto-disabled after {errors} consecutive errors",
                        },
                    )
                )
            else:
                await self._event_bus.publish(
                    Event(
                        type=EventType.SCHEDULE_ERROR,
                        data={
                            "schedule_id": schedule_id,
                            "consecutive_errors": errors,
                            "next_retry_seconds": backoff,
                        },
                    )
                )

    # ------------------------------------------------------------------
    # Schedule computation
    # ------------------------------------------------------------------

    def _compute_next_fire(
        self,
        sched: Any,
        after: datetime,
    ) -> datetime | None:
        """Compute the next fire time for a schedule."""
        stype = sched.schedule_type

        if stype == "cron" and sched.cron_expr:
            return self._next_cron(sched.cron_expr, sched.timezone, after)

        if stype == "interval" and sched.interval_seconds:
            return after + timedelta(seconds=sched.interval_seconds)

        if stype == "one_shot":
            # One-shot fires once; if already fired, no next
            if sched.last_fired_at is not None:
                return None
            if sched.one_shot_at and sched.one_shot_at > after:
                return sched.one_shot_at
            # Due now or in the past
            return after

        return None

    @staticmethod
    def _next_cron(expr: str, tz_name: str, after: datetime) -> datetime:
        """Compute the next cron fire time in the given timezone."""
        try:
            tz = timezone(timedelta(hours=0)) if tz_name == "UTC" else _resolve_tz(tz_name)
        except Exception:
            tz = timezone(timedelta(hours=0))

        local_after = after.astimezone(tz)
        cron = croniter(expr, local_after)
        next_local: datetime = cron.get_next(datetime)  # type: ignore[assignment]
        return next_local.astimezone(UTC).replace(tzinfo=UTC)

    @staticmethod
    def _compute_backoff_delay(consecutive_errors: int) -> int:
        """Return backoff delay in seconds for the given error count."""
        idx = min(consecutive_errors - 1, len(_BACKOFF_SCHEDULE) - 1)
        return _BACKOFF_SCHEDULE[max(idx, 0)]

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    async def _initialise_next_fire_times(self) -> None:
        """Ensure every enabled schedule has a next_fire_at value."""
        now = datetime.now(UTC)
        async with self._db_session() as db:
            schedules = await list_schedules(db, enabled=True)
            for sched in schedules:
                if sched.next_fire_at is not None:
                    continue
                next_fire = self._compute_next_fire(sched, now)
                if next_fire is not None:
                    await update_schedule(db, sched.schedule_id, next_fire_at=next_fire)
            await db.commit()

    async def _catch_up_missed(self) -> None:
        """Fire schedules that were due during downtime (capped + staggered)."""
        now = datetime.now(UTC)
        async with self._db_session() as db:
            due = await list_due_schedules(db, now)

        fired = 0
        for sched_row in due:
            if fired >= self._max_missed_on_startup:
                logger.info(
                    "Missed-job catchup capped at %d; deferring remaining",
                    self._max_missed_on_startup,
                )
                break
            await self._fire_schedule(sched_row.schedule_id)
            fired += 1
            if fired < len(due) and self._missed_stagger_seconds > 0:
                await asyncio.sleep(self._missed_stagger_seconds)

        if fired > 0:
            logger.info("Caught up %d missed schedule(s) on startup", fired)

    # ------------------------------------------------------------------
    # Dynamic management (called by API routes)
    # ------------------------------------------------------------------

    async def notify_schedule_changed(self, schedule_id: str) -> None:
        """Recompute next_fire_at for a schedule and wake the timer."""
        now = datetime.now(UTC)
        async with self._db_session() as db:
            sched = await get_schedule(db, schedule_id)
            if sched is None:
                self.wake()
                return
            if sched.enabled:
                next_fire = self._compute_next_fire(sched, now)
                await update_schedule(db, schedule_id, next_fire_at=next_fire)
            else:
                await update_schedule(db, schedule_id, next_fire_at=None)
            await db.commit()
        self.wake()

    # ------------------------------------------------------------------
    # DB helper
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _db_session(self) -> AsyncIterator[Any]:
        """Yield an async DB session."""
        async with self._session_factory() as session:
            yield session


def _resolve_tz(name: str) -> timezone | ZoneInfo:
    """Resolve a timezone name while preserving DST-aware IANA semantics."""

    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=0))
