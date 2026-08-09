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
from sqlalchemy.exc import IntegrityError

from cognis.core.events import Event, EventBus, EventType
from cognis.core.workflow_management import (
    delete_materialized_workflow,
    get_attached_skill_workflow_source,
    materialize_skill_workflow,
)
from cognis.logging import get_logger
from cognis.models.task import TaskDelivery
from cognis.models.workflow import CompletionDeliveryPolicy
from cognis.store.coordination import DatabaseLeaseStore, database_now
from cognis.store.queries import (
    count_active_tasks_for_schedule,
    delete_schedule,
    get_agent,
    get_schedule,
    get_task,
    list_due_schedules,
    list_schedules,
    update_schedule,
    update_schedule_fire_state,
)
from cognis.store.schedule_fires import ScheduleFireStore

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
        controller_owner_id: str = "simple-controller",
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
        self._controller_owner_id = controller_owner_id
        self._lease_store = DatabaseLeaseStore(session_factory)
        self._fire_store = ScheduleFireStore(session_factory)
        self._manual_triggers: dict[str, asyncio.Task[str | None]] = {}
        event_bus.subscribe(EventType.TASK_COMPLETED, self._handle_task_terminal_event)
        event_bus.subscribe(EventType.TASK_FAILED, self._handle_task_terminal_event)
        event_bus.subscribe(EventType.TASK_CANCELLED, self._handle_task_terminal_event)

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
        if hasattr(self, "_fire_store") and await self._fire_store.catchup_active():
            await self._catch_up_missed(resume_only=True)
            return
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

    async def trigger_now(self, schedule_id: str) -> str | None:
        """Run a schedule immediately without consuming its recurring occurrence."""
        active = self._manual_triggers.get(schedule_id)
        if active is not None:
            return await asyncio.shield(active)
        trigger = asyncio.create_task(
            self._trigger_now_owned(schedule_id),
            name=f"schedule-manual-trigger:{schedule_id}",
        )
        self._manual_triggers[schedule_id] = trigger
        try:
            return await asyncio.shield(trigger)
        finally:
            if self._manual_triggers.get(schedule_id) is trigger:
                self._manual_triggers.pop(schedule_id, None)

    async def _trigger_now_owned(self, schedule_id: str) -> str | None:
        """Own one coalesced manual trigger for this controller."""
        lease = await self._lease_store.acquire(
            f"schedule:{schedule_id}",
            self._controller_owner_id,
            ttl_seconds=60,
        )
        if lease is None:
            return None
        lease_lost = asyncio.Event()
        renew_task = asyncio.create_task(
            self._renew_fire_lease(lease, lease_lost),
            name=f"schedule-manual-fire-renew:{schedule_id}",
        )
        dispatched = False
        try:
            claim = await self._fire_store.claim_manual(
                schedule_id=schedule_id,
                lease=lease,
            )
            if claim is None:
                return None
            sched = claim.schedule
            if claim.status == "skipped":
                return None
            if claim.status == "dispatched":
                await self._activate_manual_task(claim.task_id)
                return claim.task_id

            existing = None
            async with self._db_session() as db:
                existing = await get_task(db, claim.task_id)
            if existing is None:
                if lease_lost.is_set():
                    return None
                try:
                    task = await self._dispatch_claimed_fire(
                        sched,
                        claim.scheduled_fire_at,
                        claim.task_id,
                        status="draft",
                    )
                except IntegrityError:
                    async with self._db_session() as db:
                        task = await get_task(db, claim.task_id)
                    if task is None:
                        raise
            else:
                task = existing
            if not await self._fire_store.link_manual_task(claim=claim, lease=lease):
                return None
            reconcile = getattr(self._task_queue, "reconcile_submitted", None)
            if reconcile is not None:
                reconciled = await reconcile(claim.task_id)
                if reconciled is not None:
                    task = reconciled

            task_status = getattr(task, "status", None)
            if getattr(task_status, "value", task_status) == "draft":
                task = await self._activate_manual_task(claim.task_id)
            if not await self._fire_store.settle_manual_dispatched(
                claim=claim,
                lease=lease,
                one_shot_status="success",
            ):
                return None
            dispatched = True
            await self._event_bus.publish(
                Event(
                    type=EventType.SCHEDULE_FIRED,
                    data={"schedule_id": schedule_id, "task_id": claim.task_id},
                )
            )
            return claim.task_id
        except Exception as exc:
            if "claim" in locals() and claim is not None and not dispatched:
                async with self._db_session() as db:
                    durable_task = await get_task(db, claim.task_id)
                if durable_task is None:
                    await self._fire_store.mark_manual_failed(
                        claim=claim,
                        lease=lease,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            logger.exception("Schedule %s manual fire failed", schedule_id)
            return None
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            await self._lease_store.release(lease)

    async def _activate_manual_task(self, task_id: str) -> Any:
        async with self._db_session() as db:
            task = await get_task(db, task_id)
        if task is None:
            raise RuntimeError(f"Reserved manual task {task_id} does not exist")
        task_status = getattr(task, "status", None)
        if getattr(task_status, "value", task_status) != "draft":
            return task
        submit_existing = getattr(self._task_queue, "submit_existing", None)
        if submit_existing is None:
            raise RuntimeError("Task queue cannot activate a reserved manual task")
        return await submit_existing(task_id)

    async def _fire_schedule(self, schedule_id: str) -> str | None:
        """Claim, dispatch, and settle one durable logical schedule fire."""
        if not hasattr(self, "_lease_store"):
            return await self._fire_schedule_uncoordinated(schedule_id)

        lease = await self._lease_store.acquire(
            f"schedule:{schedule_id}",
            self._controller_owner_id,
            ttl_seconds=60,
        )
        if lease is None:
            return None
        async with self._db_session() as db:
            sched = await get_schedule(db, schedule_id)
        if sched is None or not sched.enabled:
            await self._lease_store.release(lease)
            return None
        scheduled_fire_at = sched.next_fire_at
        if scheduled_fire_at is None:
            scheduled_fire_at = datetime.now(UTC)
            async with self._db_session() as db:
                updated = await update_schedule(
                    db,
                    schedule_id,
                    next_fire_at=scheduled_fire_at,
                )
                await db.commit()
            if updated is None or not updated.enabled:
                await self._lease_store.release(lease)
                return None
            async with self._db_session() as db:
                sched = await get_schedule(db, schedule_id)
            if sched is None or sched.next_fire_at is None:
                await self._lease_store.release(lease)
                return None
            scheduled_fire_at = sched.next_fire_at
        lease_lost = asyncio.Event()
        renew_task = asyncio.create_task(
            self._renew_fire_lease(lease, lease_lost),
            name=f"schedule-fire-renew:{schedule_id}",
        )
        dispatched = False
        try:
            claim = await self._fire_store.claim(
                schedule_id=schedule_id,
                scheduled_fire_at=scheduled_fire_at,
                lease=lease,
            )
            if claim is None:
                return None
            sched = claim.schedule
            if claim.status == "skipped":
                await self._advance_claimed_fire(
                    sched,
                    scheduled_fire_at,
                    status="skipped",
                    lease=lease,
                )
                return None
            if not claim.should_dispatch:
                await self._advance_claimed_fire(
                    sched,
                    scheduled_fire_at,
                    status="success",
                    lease=lease,
                )
                return claim.task_id if claim.status == "dispatched" else None

            existing = None
            async with self._db_session() as db:
                existing = await get_task(db, claim.task_id)
            if existing is None:
                if lease_lost.is_set():
                    return None
                try:
                    task = await self._dispatch_claimed_fire(
                        sched,
                        scheduled_fire_at,
                        claim.task_id,
                    )
                except IntegrityError:
                    async with self._db_session() as db:
                        task = await get_task(db, claim.task_id)
                    if task is None:
                        raise
            else:
                task = existing
            reconcile = getattr(self._task_queue, "reconcile_submitted", None)
            if reconcile is not None:
                reconciled = await reconcile(claim.task_id)
                if reconciled is not None:
                    task = reconciled

            if not await self._fire_store.mark_dispatched(claim=claim, lease=lease):
                return None
            dispatched = True
            await self._advance_claimed_fire(
                sched,
                scheduled_fire_at,
                status="success",
                lease=lease,
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.SCHEDULE_FIRED,
                    data={"schedule_id": schedule_id, "task_id": claim.task_id},
                )
            )
            return claim.task_id
        except Exception as exc:
            if "claim" in locals() and claim is not None and not dispatched:
                errors = int(sched.consecutive_errors or 0) + 1
                disabled = errors >= self._max_consecutive_errors
                backoff = self._compute_backoff_delay(errors)
                disabled_reason = (
                    f"Auto-disabled after {errors} consecutive errors" if disabled else None
                )
                settled = await self._fire_store.mark_failed(
                    claim=claim,
                    lease=lease,
                    error=f"{type(exc).__name__}: {exc}",
                    next_fire_at=(
                        None if disabled else datetime.now(UTC) + timedelta(seconds=backoff)
                    ),
                    consecutive_errors=errors,
                    disabled_reason=disabled_reason,
                )
                if settled:
                    await self._event_bus.publish(
                        Event(
                            type=(
                                EventType.SCHEDULE_DISABLED
                                if disabled
                                else EventType.SCHEDULE_ERROR
                            ),
                            data={
                                "schedule_id": schedule_id,
                                "consecutive_errors": errors,
                                "next_retry_seconds": backoff,
                                "created_by": sched.created_by,
                                "agent_id": sched.agent_id,
                                "schedule_name": sched.name,
                                "error": f"{type(exc).__name__}: {exc}",
                                **(
                                    {"reason": disabled_reason}
                                    if disabled_reason is not None
                                    else {}
                                ),
                            },
                        )
                    )
            logger.exception("Schedule %s durable fire failed", schedule_id)
            return None
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            await self._lease_store.release(lease)

    async def _renew_fire_lease(self, lease: Any, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                renewed = await self._lease_store.renew(lease, ttl_seconds=60)
            except Exception:
                renewed = None
            if renewed is None:
                lease_lost.set()
                return

    async def _dispatch_claimed_fire(
        self,
        sched: Any,
        scheduled_fire_at: datetime,
        task_id: str,
        *,
        status: str = "queued",
    ) -> Any:
        template: dict[str, Any] = dict(sched.task_template)
        title = template.pop("title", sched.name)
        description = template.pop("description", sched.description or "")
        expected_output = template.pop("expected_output", None)
        priority = template.pop("priority", 0)
        template.pop("workflow_id", None)
        template.pop("skill_id", None)
        workflow_id = sched.workflow_id
        created_workflow_id: str | None = None
        workspace_root = template.pop("workspace_root", None)
        working_directory = template.pop("working_directory", None)
        session_policy = template.pop("session_policy", None)
        template.pop("created_by_agent_id", None)
        if getattr(sched, "skill_id", None):
            async with self._db_session() as db:
                agent_row = await get_agent(db, sched.agent_id)
            if agent_row is None:
                raise ValueError(f"Agent '{sched.agent_id}' not found for schedule")
            await get_attached_skill_workflow_source(
                session_factory=self._db_session,
                owner_email=sched.created_by,
                agent=agent_row,
                skill_id=str(sched.skill_id),
            )
            created_workflow = await materialize_skill_workflow(
                session_factory=self._db_session,
                owner_email=sched.created_by,
                skill_id=str(sched.skill_id),
                lifecycle="ephemeral",
                composition_source="manual",
                composition_intent=str(description or title),
            )
            workflow_id = created_workflow.workflow_id
            created_workflow_id = created_workflow.workflow_id

        delivery_raw = template.pop("delivery", None)
        delivery: TaskDelivery | None = TaskDelivery(mode="preferred_channel")
        if isinstance(delivery_raw, dict):
            delivery = TaskDelivery(**delivery_raw)
        completion_delivery = CompletionDeliveryPolicy(
            completion_mode_family=getattr(sched, "completion_mode_family", "default"),
            allow_silent_completion=bool(getattr(sched, "allow_silent_completion", False)),
        )
        try:
            return await self._task_queue.submit(
                task_id=task_id,
                created_by=sched.created_by,
                agent_id=sched.agent_id,
                agent_profile_id=getattr(sched, "agent_profile_id", None),
                title=str(title),
                description=str(description),
                expected_output=expected_output,
                priority=int(priority),
                source_type="scheduler",
                source_ref=sched.schedule_id,
                delivery=delivery,
                completion_delivery=completion_delivery,
                interaction_mode_override=getattr(sched, "interaction_mode_override", None)
                or "none",
                session_policy=session_policy if isinstance(session_policy, dict) else None,
                workflow_id=workflow_id,
                project_id=getattr(sched, "project_id", None),
                workspace_root=workspace_root,
                working_directory=working_directory,
                scheduled_for=scheduled_fire_at,
                status=status,
            )
        except BaseException:
            if created_workflow_id is not None:
                async with self._db_session() as db:
                    committed_task = await get_task(db, task_id)
                if committed_task is None:
                    with contextlib.suppress(Exception):
                        await delete_materialized_workflow(
                            session_factory=self._db_session,
                            workflow_id=created_workflow_id,
                        )
            raise

    async def _advance_claimed_fire(
        self,
        sched: Any,
        scheduled_fire_at: datetime,
        *,
        status: str,
        lease: Any,
    ) -> None:
        next_fire = (
            None
            if sched.schedule_type == "one_shot"
            else self._compute_next_fire(sched, scheduled_fire_at)
        )
        advanced = await self._fire_store.advance_schedule(
            schedule_id=sched.schedule_id,
            scheduled_fire_at=scheduled_fire_at,
            next_fire_at=next_fire,
            status=status,
            consecutive_errors=sched.consecutive_errors,
            lease=lease,
        )
        if advanced and sched.delete_after_run and sched.schedule_type == "one_shot":
            async with self._db_session() as db:
                await delete_schedule(db, sched.schedule_id)
                await db.commit()

    async def _fire_schedule_uncoordinated(self, schedule_id: str) -> str | None:
        """Create a task from the schedule template and return its task id."""
        now = datetime.now(UTC)

        async with self._db_session() as db:
            sched = await get_schedule(db, schedule_id)
            if sched is None or not sched.enabled:
                return None

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
                return None

        # Create the task (outside the schedule DB session)
        try:
            template: dict[str, Any] = dict(sched.task_template)
            title = template.pop("title", sched.name)
            description = template.pop("description", sched.description or "")
            expected_output = template.pop("expected_output", None)
            priority = template.pop("priority", 0)
            template.pop("workflow_id", None)
            template.pop("skill_id", None)
            workflow_id = sched.workflow_id
            skill_id = getattr(sched, "skill_id", None)
            workspace_root = template.pop("workspace_root", None)
            working_directory = template.pop("working_directory", None)
            session_policy = template.pop("session_policy", None)
            template.pop("created_by_agent_id", None)
            created_workflow_id: str | None = None
            created_task_id: str | None = None

            if skill_id:
                async with self._db_session() as db:
                    agent_row = await get_agent(db, sched.agent_id)
                if agent_row is None:
                    raise ValueError(f"Agent '{sched.agent_id}' not found for schedule")
                await get_attached_skill_workflow_source(
                    session_factory=self._db_session,
                    owner_email=sched.created_by,
                    agent=agent_row,
                    skill_id=str(skill_id),
                )
                created_workflow = await materialize_skill_workflow(
                    session_factory=self._db_session,
                    owner_email=sched.created_by,
                    skill_id=str(skill_id),
                    lifecycle="ephemeral",
                    composition_source="manual",
                    composition_intent=str(description or title),
                )
                workflow_id = created_workflow.workflow_id
                created_workflow_id = created_workflow.workflow_id

            # Delivery config from template
            delivery_raw = template.pop("delivery", None)
            delivery: TaskDelivery | None = TaskDelivery(mode="preferred_channel")
            if isinstance(delivery_raw, dict):
                delivery = TaskDelivery(**delivery_raw)
            completion_delivery = CompletionDeliveryPolicy(
                completion_mode_family=getattr(sched, "completion_mode_family", "default"),
                allow_silent_completion=bool(getattr(sched, "allow_silent_completion", False)),
            )

            task = await self._task_queue.submit(
                created_by=sched.created_by,
                agent_id=sched.agent_id,
                agent_profile_id=getattr(sched, "agent_profile_id", None),
                title=str(title),
                description=str(description),
                expected_output=expected_output,
                priority=int(priority),
                source_type="scheduler",
                source_ref=schedule_id,
                delivery=delivery,
                completion_delivery=completion_delivery,
                interaction_mode_override=getattr(sched, "interaction_mode_override", None)
                or "none",
                session_policy=session_policy if isinstance(session_policy, dict) else None,
                workflow_id=workflow_id,
                project_id=getattr(sched, "project_id", None),
                workspace_root=workspace_root,
                working_directory=working_directory,
                scheduled_for=now,
            )
            created_task_id = task.task_id

            logger.info(
                "Schedule %s fired -> task %s",
                schedule_id,
                task.task_id,
            )

            async with self._db_session() as db:
                current_task = await get_task(db, task.task_id)
                current_task_status = (
                    str(getattr(current_task, "status", "") or "").lower()
                    if current_task is not None
                    else ""
                )
                if current_task_status not in {"completed", "failed", "cancelled"}:
                    next_fire = self._compute_next_fire(sched, now)
                    await update_schedule_fire_state(
                        db,
                        schedule_id,
                        last_fired_at=now,
                        next_fire_at=next_fire,
                        last_run_status="success",
                        consecutive_errors=sched.consecutive_errors,
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
            return task.task_id

        except Exception as exc:
            if created_workflow_id is not None and created_task_id is None:
                with contextlib.suppress(Exception):
                    await delete_materialized_workflow(
                        session_factory=self._db_session,
                        workflow_id=created_workflow_id,
                    )
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
                            "created_by": sched.created_by,
                            "agent_id": sched.agent_id,
                            "schedule_name": sched.name,
                            "error": f"{type(exc).__name__}: {exc}",
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
                            "created_by": sched.created_by,
                            "agent_id": sched.agent_id,
                            "schedule_name": sched.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                )
            return None

    async def _handle_task_terminal_event(self, event: Event) -> None:
        """Propagate terminal scheduler-created task status back to the schedule."""

        task_id = event.data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return

        async with self._db_session() as db:
            task = await get_task(db, task_id)
            if task is None or task.source_type != "scheduler" or not task.source_ref:
                return
            if await self._fire_store.is_manual_task(task_id):
                return
            schedule_id = str(task.source_ref)
            sched = await get_schedule(db, schedule_id)
            if sched is None:
                return

        fire_at = _task_fire_at(task)
        if _task_terminal_event_is_stale(fire_at, sched.last_fired_at):
            logger.info(
                "Ignoring stale terminal event for scheduled task %s",
                task_id,
                extra={
                    "extra_data": {
                        "schedule_id": schedule_id,
                        "task_fire_at": fire_at.isoformat()
                        if isinstance(fire_at, datetime)
                        else None,
                        "last_fired_at": sched.last_fired_at.isoformat()
                        if isinstance(sched.last_fired_at, datetime)
                        else None,
                    }
                },
            )
            return

        status = str(getattr(task, "status", "") or "").lower()
        recorded_fire_at = fire_at if isinstance(fire_at, datetime) else datetime.now(UTC)
        if status == "completed":
            next_fire = (
                None
                if sched.schedule_type == "one_shot"
                else self._compute_next_fire(sched, recorded_fire_at)
            )
            async with self._db_session() as db:
                await update_schedule_fire_state(
                    db,
                    schedule_id,
                    last_fired_at=recorded_fire_at,
                    next_fire_at=next_fire,
                    last_run_status="success",
                    consecutive_errors=0,
                )
                await db.commit()
            return

        if status not in {"failed", "cancelled"}:
            return

        now = datetime.now(UTC)
        errors = int(sched.consecutive_errors or 0) + 1
        disabled = errors >= self._max_consecutive_errors
        backoff = self._compute_backoff_delay(errors)
        disabled_reason = f"Auto-disabled after {errors} consecutive errors" if disabled else None
        async with self._db_session() as db:
            await update_schedule_fire_state(
                db,
                schedule_id,
                last_fired_at=recorded_fire_at,
                next_fire_at=now + timedelta(seconds=backoff) if not disabled else None,
                last_run_status="failed",
                consecutive_errors=errors,
                disabled_reason=disabled_reason,
                enabled=False if disabled else None,
            )
            await db.commit()

        if disabled:
            logger.warning(
                "Schedule %s auto-disabled after task %s failed",
                schedule_id,
                task_id,
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.SCHEDULE_DISABLED,
                    data={
                        "schedule_id": schedule_id,
                        "reason": disabled_reason,
                        "created_by": sched.created_by,
                        "agent_id": sched.agent_id,
                        "schedule_name": sched.name,
                        "task_id": task_id,
                        "error": task.result_summary,
                    },
                )
            )
            return

        await self._event_bus.publish(
            Event(
                type=EventType.SCHEDULE_ERROR,
                data={
                    "schedule_id": schedule_id,
                    "consecutive_errors": errors,
                    "next_retry_seconds": backoff,
                    "created_by": sched.created_by,
                    "agent_id": sched.agent_id,
                    "schedule_name": sched.name,
                    "task_id": task_id,
                    "error": task.result_summary,
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

    async def _catch_up_missed(self, *, resume_only: bool = False) -> None:
        """Fire a bounded missed backlog, then jump recurring schedules forward."""
        catchup_lease = None
        while catchup_lease is None and not self._stop_event.is_set():
            catchup_lease = await self._lease_store.acquire(
                "scheduler:startup-catchup",
                self._controller_owner_id,
                ttl_seconds=60,
            )
            if catchup_lease is None:
                await asyncio.sleep(0.2)
        if catchup_lease is None:
            return
        lease_lost = asyncio.Event()
        renew_task = asyncio.create_task(
            self._renew_fire_lease(catchup_lease, lease_lost),
            name="scheduler-catchup-renew",
        )
        fired = 0
        try:
            async with self._db_session() as db:
                proposed_cutoff = await database_now(db)
            catchup_state = await self._fire_store.prepare_catchup(
                lease=catchup_lease,
                cutoff_at=proposed_cutoff,
                budget=self._max_missed_on_startup,
                start_new=not resume_only,
            )
            if catchup_state is None:
                if await self._fire_store.catchup_active():
                    lease_lost.set()
                else:
                    return
            cutoff, remaining_budget = catchup_state or (proposed_cutoff, 0)
            while remaining_budget > 0 and not lease_lost.is_set():
                async with self._db_session() as db:
                    due = await list_due_schedules(db, cutoff)
                if not due:
                    break
                for sched_row in due:
                    if remaining_budget <= 0 or lease_lost.is_set():
                        break
                    reservation = await self._fire_store.reserve_catchup_fire(lease=catchup_lease)
                    if reservation is None:
                        lease_lost.set()
                        break
                    cutoff, remaining_budget = reservation
                    original_fire = sched_row.next_fire_at
                    if original_fire is not None and original_fire.tzinfo is None:
                        original_fire = original_fire.replace(tzinfo=UTC)
                    await self._fire_schedule(sched_row.schedule_id)
                    async with self._db_session() as db:
                        after_fire = await get_schedule(db, sched_row.schedule_id)
                    next_fire = after_fire.next_fire_at if after_fire is not None else None
                    if next_fire is not None and next_fire.tzinfo is None:
                        next_fire = next_fire.replace(tzinfo=UTC)
                    settled = (
                        after_fire is None or not after_fire.enabled or next_fire != original_fire
                    )
                    if settled:
                        fired += 1
                    else:
                        restored = await self._fire_store.restore_catchup_fire(lease=catchup_lease)
                        if restored is None:
                            lease_lost.set()
                            break
                        remaining_budget = restored
                        await asyncio.sleep(0.2)
                if remaining_budget > 0 and self._missed_stagger_seconds > 0:
                    await asyncio.sleep(self._missed_stagger_seconds)

            if not lease_lost.is_set():
                while not lease_lost.is_set():
                    async with self._db_session() as db:
                        fresh_now = await database_now(db)
                        remaining_due = await list_due_schedules(db, fresh_now)
                    pending = False
                    for sched_row in remaining_due:
                        normalized = await self._skip_missed_backlog(
                            sched_row.schedule_id, fresh_now
                        )
                        if normalized is False:
                            pending = True
                    if not pending:
                        if not await self._fire_store.complete_catchup(lease=catchup_lease):
                            lease_lost.set()
                        break
                    await asyncio.sleep(0.2)

            if fired > 0:
                logger.info("Caught up %d missed schedule(s) on startup", fired)
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            await self._lease_store.release(catchup_lease)
        if lease_lost.is_set() and not self._stop_event.is_set():
            await self._catch_up_missed(resume_only=True)

    async def _skip_missed_backlog(self, schedule_id: str, now: datetime) -> bool | None:
        """CAS an overdue recurring schedule to its first future occurrence."""
        lease = await self._lease_store.acquire(
            f"schedule:{schedule_id}",
            self._controller_owner_id,
            ttl_seconds=60,
        )
        if lease is None:
            return False
        try:
            async with self._db_session() as db:
                sched = await get_schedule(db, schedule_id)
            current_fire = (
                sched.next_fire_at.replace(tzinfo=UTC)
                if sched is not None
                and sched.next_fire_at is not None
                and sched.next_fire_at.tzinfo is None
                else (sched.next_fire_at if sched is not None else None)
            )
            if (
                sched is None
                or not sched.enabled
                or current_fire is None
                or current_fire > now
                or sched.schedule_type == "one_shot"
            ):
                return None if sched is None or sched.schedule_type == "one_shot" else True
            next_fire = self._compute_next_fire(sched, now)
            advanced = await self._fire_store.advance_schedule(
                schedule_id=schedule_id,
                scheduled_fire_at=current_fire,
                next_fire_at=next_fire,
                status=sched.last_run_status or "skipped",
                consecutive_errors=sched.consecutive_errors,
                lease=lease,
            )
            if advanced:
                return True
            async with self._db_session() as db:
                current = await get_schedule(db, schedule_id)
            if current is None or not current.enabled:
                return True
            current_next = current.next_fire_at
            if current_next is not None and current_next.tzinfo is None:
                current_next = current_next.replace(tzinfo=UTC)
            return bool(current_next is None or current_next > now)
        finally:
            await self._lease_store.release(lease)

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


def _task_fire_at(task: Any) -> datetime | None:
    """Return the schedule fire timestamp carried by a scheduler-created task."""

    scheduled_for = getattr(task, "scheduled_for", None)
    if isinstance(scheduled_for, datetime):
        return scheduled_for
    created_at = getattr(task, "created_at", None)
    if isinstance(created_at, datetime):
        return created_at
    return None


def _task_terminal_event_is_stale(
    task_fire_at: Any,
    schedule_last_fired_at: Any,
) -> bool:
    """Return true when a terminal task event predates the latest schedule fire."""

    if not isinstance(task_fire_at, datetime) or not isinstance(schedule_last_fired_at, datetime):
        return False
    try:
        return task_fire_at < schedule_last_fired_at
    except TypeError:
        return False


def _resolve_tz(name: str) -> timezone | ZoneInfo:
    """Resolve a timezone name while preserving DST-aware IANA semantics."""

    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=0))
