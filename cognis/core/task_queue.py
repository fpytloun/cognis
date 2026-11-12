"""Task queue — priority-based task picking, dependency resolution, and lifecycle.

Uses local and cluster wakeups with bounded fallback reconciliation. Capacity enforcement
uses a unified model: max active steps globally and per-agent.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import PauseResolution
from cognis.core.events import Event, EventBus, EventType
from cognis.core.feedback.admission import FeedbackTaskAdmission
from cognis.core.feedback.primitives import FeedbackError, affected
from cognis.core.task_execution import (
    StaleTaskExecutionOwner,
    TaskExecutionClaim,
    TaskExecutionFence,
    TaskExecutionStore,
    assert_task_execution_fence,
    bind_task_execution_fence,
    reset_task_execution_fence,
)
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.logging import get_logger
from cognis.models.session import next_event_page_after_seq
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import (
    CompletionDeliveryPolicy,
    SessionPolicy,
    Workflow,
    WorkflowState,
    pin_effective_workflow,
)
from cognis.runtime_context import scoped_runtime_context
from cognis.store.feedback_models import TaskRevisionReceiptRow
from cognis.store.models import NotificationRow, Task
from cognis.store.queries import (
    add_task_dependency,
    assert_task_attempt_current,
    create_task,
    defer_running_task,
    fail_orphaned_running_step_runs,
    fail_running_step_runs_for_task,
    get_dependent_tasks,
    get_setting_value,
    get_task,
    get_task_dependencies,
    get_unmet_dependencies,
    list_active_managed_conversation_links_for_controller_session,
    list_step_runs_for_task,
    list_tasks_by_status,
    supersede_step_runs_for_revision,
    update_managed_conversation_link,
    update_task_status,
    update_task_workflow_state,
    update_workflow,
)

if TYPE_CHECKING:
    from cognis.providers.executor.websocket import WebSocketExecutorProvider

TaskCreationParticipant = Callable[[AsyncSession, Task], Awaitable[None]]

logger = get_logger(__name__)

# Prometheus metrics
QUEUE_DEPTH = Gauge(
    "cognis_task_queue_depth",
    "Tasks waiting in queue",
    labelnames=("queue",),
)
TASKS_TOTAL = Counter(
    "cognis_tasks_total",
    "Task lifecycle transitions",
    labelnames=("status",),
)
TASK_PICK_DURATION = Histogram(
    "cognis_task_pick_duration_seconds",
    "Time to pick next task",
)
TASK_RECOVERY_CLASSIFICATIONS = Counter(
    "cognis_task_recovery_classifications_total",
    "Task crash-recovery classifications.",
    labelnames=("classification",),
)

# Default capacity limits
DEFAULT_MAX_ACTIVE_STEPS_GLOBAL = 10
DEFAULT_MAX_ACTIVE_STEPS_PER_AGENT = 5
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
QUEUE_RECONCILIATIONS = Counter(
    "cognis_task_queue_reconciliations_total",
    "Queue reconciliation passes by bounded wake reason.",
    ("reason",),
)
QUEUE_CLAIM_ATTEMPTS = Counter(
    "cognis_task_queue_claim_attempts_total",
    "Durable queue claim attempts.",
)
DEFAULT_STALE_AFTER_SECONDS = 300

_RERUN_CLONE_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


@dataclass(slots=True)
class TaskRerunResult:
    """Resolved outcome of a task rerun request."""

    source_task_id: str
    task: TaskModel
    created_new: bool


@dataclass(slots=True)
class TaskRevisionResult:
    """Durable revision outcome, including whether this controller relaunched it."""

    task: TaskModel
    target_step: str
    superseded_count: int
    relaunched: bool
    admitted_attempt_number: int | None = None

    @property
    def status(self) -> TaskStatus:
        return self.task.status

    @property
    def attempt_number(self) -> int:
        return self.admitted_attempt_number or self.task.attempt_number


def _recover_pause_options(value: Any) -> list[dict[str, Any]] | None:
    """Normalize recovered pause options into canonical list[dict] shape."""

    if not isinstance(value, list):
        return None
    normalized: list[dict[str, Any]] = []
    for option in value:
        if isinstance(option, dict):
            normalized.append(option)
        elif isinstance(option, str):
            normalized.append({"label": option, "action": option})
    return normalized or None


def _recover_pause_context(value: Any) -> dict[str, Any] | None:
    """Normalize recovered pause context into dict-or-None."""

    if isinstance(value, dict):
        return value
    if value is None or value == "":
        return None
    return {"note": str(value)}


class TaskQueue:
    """Priority-based task queue with dependency resolution."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_engine: WorkflowEngine,
        workflow_registry: WorkflowRegistry,
        event_bus: EventBus,
        agent_registry: Any = None,
        websocket_provider: WebSocketExecutorProvider | None = None,
        controller_owner_id: str = "simple-controller",
        max_active_steps_global: int = DEFAULT_MAX_ACTIVE_STEPS_GLOBAL,
        max_active_steps_per_agent: int = DEFAULT_MAX_ACTIVE_STEPS_PER_AGENT,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._workflow_engine = workflow_engine
        self._workflow_registry = workflow_registry
        self._event_bus = event_bus
        self._agent_registry = agent_registry
        self._websocket_provider = websocket_provider
        self._execution_store = TaskExecutionStore(
            session_factory,
            owner_id=controller_owner_id,
            max_active_global=max_active_steps_global,
            max_active_per_agent=max_active_steps_per_agent,
        )
        self._max_active_global = max_active_steps_global
        self._max_active_per_agent = max_active_steps_per_agent
        self._poll_interval = poll_interval_seconds
        self._accepting = True
        self._stop_event = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None
        self._cancellation_task: asyncio.Task[None] | None = None
        self._cancel_dispatch_wakeup = asyncio.Event()
        self._active_runs: dict[str, asyncio.Task[Any]] = {}
        self._task_claims: dict[str, TaskExecutionClaim] = {}
        self._run_controls: dict[str, asyncio.Event] = {}
        self._pick_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._next_recovery_at = 0.0
        self._turn_scheduler: Any = None
        self.cluster_signals: Any = None

    async def _on_cluster_change(self, event: Event) -> None:
        self._cancel_dispatch_wakeup.set()
        if event.data.get("kind") == "task_queue_changed":
            self._wake_event.set()

    async def _wake_workers(self) -> None:
        """Wake claimers after durable queue or capacity changes."""
        from cognis.core.queue_wakeup import publish_queue_wakeup

        self._wake_event.set()
        await publish_queue_wakeup(self._event_bus, self.cluster_signals)

    def set_turn_scheduler(self, turn_scheduler: Any) -> None:
        """Attach the durable turn controller after application wiring completes."""

        self._turn_scheduler = turn_scheduler

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_engine: WorkflowEngine,
        workflow_registry: WorkflowRegistry,
        event_bus: EventBus,
        agent_registry: Any = None,
        websocket_provider: WebSocketExecutorProvider | None = None,
        controller_owner_id: str = "simple-controller",
    ) -> TaskQueue:
        """Create a TaskQueue with DB-backed settings."""
        async with session_factory() as db_session:
            max_global = await get_setting_value(
                db_session,
                "workflow.max_active_steps_global",
                DEFAULT_MAX_ACTIVE_STEPS_GLOBAL,
            )
            max_per_agent = await get_setting_value(
                db_session,
                "workflow.max_active_steps_per_agent",
                DEFAULT_MAX_ACTIVE_STEPS_PER_AGENT,
            )
        return cls(
            session_factory=session_factory,
            workflow_engine=workflow_engine,
            workflow_registry=workflow_registry,
            event_bus=event_bus,
            agent_registry=agent_registry,
            websocket_provider=websocket_provider,
            controller_owner_id=controller_owner_id,
            max_active_steps_global=int(max_global)
            if isinstance(max_global, int)
            else DEFAULT_MAX_ACTIVE_STEPS_GLOBAL,
            max_active_steps_per_agent=int(max_per_agent)
            if isinstance(max_per_agent, int)
            else DEFAULT_MAX_ACTIVE_STEPS_PER_AGENT,
        )

    async def start(self) -> None:
        """Start the queue processing loop."""
        self._stop_event.clear()
        self._accepting = True
        self._event_bus.subscribe(EventType.CLUSTER_SCOPE_INVALIDATED, self._on_cluster_change)
        self._wake_event.set()
        self._drain_task = asyncio.create_task(self._drain_loop())
        self._cancel_dispatch_wakeup.set()
        self._cancellation_task = asyncio.create_task(self._cancelled_dispatch_loop())
        logger.info("Task queue started")

    async def stop(self) -> None:
        """Graceful shutdown.

        1. Stop accepting new tasks
        2. Signal active workflow runs to finish current LLM call
        3. Wait up to 30s for active steps to finalize
        4. Leave durable state for lease-based recovery
        5. Cancel the drain loop
        """
        self._accepting = False
        self._event_bus.unsubscribe(EventType.CLUSTER_SCOPE_INVALIDATED, self._on_cluster_change)
        self._stop_event.set()
        self._wake_event.set()

        if self._cancellation_task:
            self._cancellation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cancellation_task
            self._cancellation_task = None

        if self._drain_task:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._drain_task, timeout=5.0)

        # Cancel active runs with grace period
        if self._active_runs:
            logger.info(
                "Waiting for active workflow runs to finish",
                extra={"extra_data": {"count": len(self._active_runs)}},
            )
            for task_id, run_task in list(self._active_runs.items()):
                control = self._run_controls.get(task_id)
                if control is not None:
                    control.set()
                run_task.cancel()

            await asyncio.gather(
                *self._active_runs.values(),
                return_exceptions=True,
            )

            # Do not mutate after runs release their fences: another controller
            # may already own the task. Periodic lease recovery handles it.

        self._active_runs.clear()
        self._run_controls.clear()
        logger.info("Task queue stopped")

    async def submit(
        self,
        *,
        created_by: str,
        agent_id: str,
        title: str,
        agent_profile_id: str | None = None,
        description: str = "",
        expected_output: str | None = None,
        priority: int = 0,
        created_by_agent_id: str | None = None,
        creation_origin: str | None = None,
        source_type: str = "api",
        source_ref: str | None = None,
        source_session_id: str | None = None,
        delivery: TaskDelivery | None = None,
        completion_delivery: CompletionDeliveryPolicy | None = None,
        interaction_mode_override: str | None = None,
        escalation_timeout_seconds: int | None = None,
        session_policy: SessionPolicy | dict[str, Any] | None = None,
        workflow_id: str | None = None,
        project_id: str | None = None,
        workspace_root: str | None = None,
        working_directory: str | None = None,
        scheduled_for: datetime | None = None,
        status: str = "queued",
        task_id: str | None = None,
        creation_participant: TaskCreationParticipant | None = None,
    ) -> TaskModel:
        """Submit a task for execution.

        Creates the task in the DB and triggers dependency resolution.
        """
        if status not in {"draft", "queued", "ready"}:
            raise ValueError("Task create status must be draft, queued, or ready")
        if not self._accepting:
            raise RuntimeError("Task queue is not accepting new tasks")

        workflow_id = workflow_id or "system:general-task"
        delivery = delivery or TaskDelivery()
        completion_delivery = completion_delivery or await self._resolve_completion_delivery_policy(
            workflow_id=workflow_id,
            owner_email=created_by,
            project_id=project_id,
        )

        from cognis.models.task import validate_task_delivery

        validate_task_delivery(delivery, completion_delivery)
        async with self._session_factory() as db_session:
            if (
                source_session_id is None
                and source_type in {"chat", "agent"}
                and source_ref is not None
            ):
                from cognis.store.queries import (
                    get_latest_active_session_for_conversation,
                    lock_conversation_activity_scope,
                )

                await lock_conversation_activity_scope(db_session, source_ref)
                source_session = await get_latest_active_session_for_conversation(
                    db_session, source_ref
                )
                if source_session is None:
                    raise ValueError("Conversation-origin task requires an active source session")
                source_session_id = source_session.session_id
            row = await create_task(
                db_session,
                task_id=task_id,
                created_by=created_by,
                agent_id=agent_id,
                agent_profile_id=agent_profile_id,
                title=title,
                description=description,
                expected_output=expected_output,
                status=status,
                priority=priority,
                created_by_agent_id=created_by_agent_id,
                creation_origin="agent_composed" if created_by_agent_id else creation_origin,
                source_type=source_type,
                source_ref=source_ref,
                source_session_id=source_session_id,
                delivery_mode=delivery.mode,
                delivery_target=delivery.target,
                completion_mode_family=completion_delivery.completion_mode_family,
                allow_silent_completion=completion_delivery.allow_silent_completion,
                on_finish_instructions=completion_delivery.on_finish_instructions,
                interaction_mode_override=interaction_mode_override,
                escalation_timeout_seconds=escalation_timeout_seconds,
                session_policy=(
                    session_policy.model_dump()
                    if isinstance(session_policy, SessionPolicy)
                    else session_policy
                ),
                workflow_id=workflow_id,
                project_id=project_id,
                workspace_root=workspace_root,
                working_directory=working_directory,
                scheduled_for=scheduled_for,
            )
            if creation_participant is not None:
                await creation_participant(db_session, row)
            await db_session.commit()

            task = _row_to_task_model(row)

        TASKS_TOTAL.labels(status=status).inc()
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_CREATED,
                data={"task_id": task.task_id, "status": status},
            )
        )
        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is not None:
            await cluster_signals.publish_task_change(task.task_id)

        # If queued, try to transition to ready
        if status in {"queued", "ready"}:
            await self._try_transition_to_ready(task.task_id)
        if status == "ready":
            await self._wake_workers()

        # Wake the drain loop
        self._wake_event.set()

        return task

    async def _resolve_completion_delivery_policy(
        self,
        *,
        workflow_id: str | None,
        owner_email: str,
        project_id: str | None,
    ) -> CompletionDeliveryPolicy:
        if workflow_id is None:
            return CompletionDeliveryPolicy()

        workflow = await self._workflow_registry.get(
            workflow_id,
            owner_email=owner_email,
            project_id=project_id,
        )
        if workflow is None:
            return CompletionDeliveryPolicy()

        defaults = getattr(workflow, "defaults", None)
        delivery_defaults = (
            defaults.get("delivery")
            if isinstance(defaults, dict)
            else getattr(defaults, "delivery", None)
        )
        if delivery_defaults is None:
            return CompletionDeliveryPolicy()

        return CompletionDeliveryPolicy.model_validate(
            delivery_defaults.model_dump(mode="json")
            if hasattr(delivery_defaults, "model_dump")
            else delivery_defaults
        )

    async def reconcile_submitted(self, task_id: str) -> TaskModel | None:
        """Idempotently finish submission after a creator crash."""
        async with self._session_factory() as db_session:
            row = await get_task(db_session, task_id)
        if row is None:
            return None
        if row.status == "queued":
            await self._try_transition_to_ready(task_id)
            async with self._session_factory() as db_session:
                row = await get_task(db_session, task_id)
                if row is None:
                    return None
        await self._wake_workers()
        return _row_to_task_model(row)

    async def create_draft(
        self,
        *,
        created_by: str,
        agent_id: str,
        title: str,
        agent_profile_id: str | None = None,
        description: str = "",
        expected_output: str | None = None,
        priority: int = 0,
        created_by_agent_id: str | None = None,
        creation_origin: str | None = None,
        delivery: TaskDelivery | None = None,
        completion_delivery: CompletionDeliveryPolicy | None = None,
        interaction_mode_override: str | None = None,
        escalation_timeout_seconds: int | None = None,
        session_policy: SessionPolicy | dict[str, Any] | None = None,
        workflow_id: str | None = None,
        project_id: str | None = None,
        workspace_root: str | None = None,
        working_directory: str | None = None,
        source_type: str = "api",
        source_ref: str | None = None,
        source_session_id: str | None = None,
    ) -> TaskModel:
        """Create a draft task visible in the kanban board."""
        return await self.submit(
            created_by=created_by,
            agent_id=agent_id,
            agent_profile_id=agent_profile_id,
            title=title,
            description=description,
            expected_output=expected_output,
            priority=priority,
            created_by_agent_id=created_by_agent_id,
            creation_origin=creation_origin,
            delivery=delivery,
            completion_delivery=completion_delivery,
            interaction_mode_override=interaction_mode_override,
            escalation_timeout_seconds=escalation_timeout_seconds,
            session_policy=session_policy,
            workflow_id=workflow_id,
            project_id=project_id,
            workspace_root=workspace_root,
            working_directory=working_directory,
            source_type=source_type,
            source_ref=source_ref,
            source_session_id=source_session_id,
            status="draft",
        )

    async def submit_existing(self, task_id: str) -> TaskModel:
        """Move an existing draft task into the queue."""
        async with self._session_factory() as db_session:
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            if task_row.status != "draft":
                raise ValueError("Only draft tasks can be submitted")
            ok = await update_task_status(db_session, task_id, "queued")
            if not ok:
                raise ValueError("Task could not be submitted")
            await db_session.commit()
            task = _row_to_task_model(task_row)

        TASKS_TOTAL.labels(status="queued").inc()
        await self._try_transition_to_ready(task_id)
        self._wake_event.set()
        task.status = TaskStatus.QUEUED
        return task

    async def batch_submit(self, task_ids: list[str]) -> dict[str, Any]:
        """Submit multiple draft tasks in best-effort mode."""
        results: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        for task_id in task_ids:
            try:
                task = await self.submit_existing(task_id)
                results.append({"task_id": task.task_id, "status": task.status})
                succeeded += 1
            except ValueError as exc:
                results.append({"task_id": task_id, "status": "error", "error": str(exc)})
                failed += 1
        return {"results": results, "succeeded": succeeded, "failed": failed}

    async def pause_task(
        self,
        task_id: str,
        *,
        expected_attempt: int | None = None,
    ) -> TaskModel:
        """Pause a running task cooperatively."""
        async with self._session_factory() as db_session:
            if expected_attempt is not None and not await assert_task_attempt_current(
                db_session, task_id, expected_attempt
            ):
                raise ValueError("Task attempt changed; refresh before mutating")
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            if task_row.status != "running":
                raise ValueError("Only running tasks can be paused")
            ok = await update_task_status(db_session, task_id, "paused")
            if not ok:
                raise ValueError("Task could not be paused")
            await db_session.commit()
            task = _row_to_task_model(task_row)

        control = self._run_controls.get(task_id)
        if control is not None:
            control.set()
        return task.model_copy(update={"status": TaskStatus.PAUSED})

    async def resume_task(
        self,
        task_id: str,
        *,
        expected_attempt: int | None = None,
    ) -> TaskModel:
        """Resume a paused task when capacity is available.

        Acquires ``_pick_lock`` to prevent races with the drain loop
        that could launch duplicate task runs.
        """
        if self._get_pending_interaction(task_id) is not None:
            raise ValueError("Task is waiting for gate or step input response")

        async with self._pick_lock:
            async with self._session_factory() as db_session:
                if expected_attempt is not None and not await assert_task_attempt_current(
                    db_session, task_id, expected_attempt
                ):
                    raise ValueError("Task attempt changed; refresh before mutating")
                task_row = await get_task(db_session, task_id)
                if task_row is None:
                    raise ValueError("Task not found")
                if task_row.status != "paused":
                    raise ValueError("Only paused tasks can be resumed")
                task = _row_to_task_model(task_row)

            if not await self._has_capacity(agent_id=task.agent_id):
                raise ValueError("No execution capacity available to resume the task")

            active_run = self._active_runs.get(task_id)
            control = self._run_controls.get(task_id)
            if (
                active_run is not None
                and not active_run.done()
                and not (control is not None and control.is_set())
            ):
                async with self._session_factory() as db_session:
                    if expected_attempt is not None and not await assert_task_attempt_current(
                        db_session, task_id, expected_attempt
                    ):
                        raise ValueError("Task attempt changed; refresh before mutating")
                    if not await update_task_status(db_session, task_id, "running"):
                        raise ValueError("Task could not be resumed")
                    await db_session.commit()
                task.status = TaskStatus.RUNNING
                return task
            if active_run is not None and not active_run.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await active_run

            claim = await self._execution_store.claim_paused(task_id)
            if claim is None:
                raise ValueError("No execution capacity available to resume the task")
            async with self._session_factory() as db_session:
                if expected_attempt is not None and not await assert_task_attempt_current(
                    db_session, task_id, expected_attempt
                ):
                    await self._execution_store.release(claim)
                    raise ValueError("Task attempt changed; refresh before mutating")
                ok = await update_task_status(db_session, task_id, "running")
                if not ok:
                    await self._execution_store.release(claim)
                    raise ValueError("Task could not be resumed")
                await db_session.commit()

            task.status = TaskStatus.RUNNING
            self._launch_claimed_task_run(task, claim)
        return task

    async def retry_failed_task(
        self, task_id: str, *, preserve_step_context: bool = True
    ) -> TaskModel:
        """Reset a failed task's workflow state and re-launch it.

        Unlike ``resume_task`` (which only works for paused tasks), this
        method handles tasks in ``failed`` status. Recovery retains the
        logical attempt and saved step history by default; explicit
        fresh-attempt callers can disable context preservation.
        """
        async with self._session_factory() as db_session:
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            if task_row.status != "failed":
                raise ValueError("Only failed tasks can be retried via retry_failed_task")
        claim = await self._execution_store.claim_existing(task_id, statuses={"failed"})
        if claim is None:
            raise ValueError("Task execution ownership is unavailable")
        try:
            fence = TaskExecutionFence(self._execution_store, claim, asyncio.Event())
            fence.mark_terminal("failed")
            async with self._session_factory() as db_session:
                await fence.assert_current(db_session)
                task_row = await get_task(db_session, task_id)
                if task_row is None or task_row.status != "failed":
                    raise ValueError("Only failed tasks can be retried")
                recovery = (task_row.result_data or {}).get("execution_recovery")
                if recovery and not preserve_step_context:
                    raise ValueError(
                        "recovery_required: reconcile outstanding tool calls before a fresh "
                        "attempt, or explicitly cancel the task."
                    )
                ws = (
                    WorkflowState.model_validate(task_row.workflow_state)
                    if task_row.workflow_state
                    else WorkflowState()
                )
                ws.loop_iterations = {}
                ws.resume_failed_step = preserve_step_context
                ws.status = "running"
                ws.current_step_status = None
                ws.pending_pause_type = None
                ws.pending_pause_payload = None
                task_row.workflow_state = ws.model_dump(mode="json")
                if not preserve_step_context:
                    task_row.attempt_number += 1
                task_row.completed_at = None
                task_row.result_summary = None
                task_row.result_data = {"execution_recovery": recovery} if recovery else None
                if not await update_task_status(db_session, task_id, "running"):
                    raise ValueError("Task could not be retried")
                await db_session.commit()
                task = _row_to_task_model(task_row)
        except StaleTaskExecutionOwner as exc:
            await self._execution_store.release(claim)
            raise ValueError(
                "Task ownership changed before retry; reload its current state."
            ) from exc
        except BaseException:
            await self._execution_store.release(claim)
            raise

        task.workflow_state = ws
        claim = replace(claim, attempt_number=task.attempt_number)
        self._launch_claimed_task_run(task, claim)
        return task

    async def rerun_task(self, task_id: str) -> TaskRerunResult:
        """Re-run a task by resuming it or cloning it for a fresh run.

        Paused tasks resume in place. Terminal tasks create a fresh draft copy,
        preserve direct dependencies, then submit the new task.
        """

        async with self._session_factory() as db_session:
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            task = _row_to_task_model(task_row)

        if task.status == TaskStatus.PAUSED:
            resumed = await self.resume_task(task_id)
            return TaskRerunResult(source_task_id=task_id, task=resumed, created_new=False)

        if task.status not in _RERUN_CLONE_STATUSES:
            raise ValueError(f"Cannot rerun task in '{task.status}' status.")

        cloned = await self._clone_task_for_rerun(task)
        return TaskRerunResult(source_task_id=task_id, task=cloned, created_new=True)

    async def request_revision(
        self,
        task_id: str,
        *,
        target_step: str | None,
        instruction: str,
        expected_attempt: int | None = None,
        feedback_admission: FeedbackTaskAdmission | None = None,
    ) -> TaskRevisionResult:
        """Serialize in-place revisions with lifecycle mutations."""

        async with self._pick_lock:
            return await self._request_revision_locked(
                task_id,
                target_step=target_step,
                instruction=instruction,
                expected_attempt=expected_attempt,
                feedback_admission=feedback_admission,
            )

    async def _request_revision_locked(
        self,
        task_id: str,
        *,
        target_step: str | None,
        instruction: str,
        expected_attempt: int | None = None,
        feedback_admission: FeedbackTaskAdmission | None = None,
    ) -> TaskRevisionResult:
        """Re-enter a workflow from a human-selected step in-place."""

        pending_pause = None
        async with self._session_factory() as db_session:
            if feedback_admission is not None:
                receipt = await db_session.get(
                    TaskRevisionReceiptRow, feedback_admission.revision_request_id
                )
                if receipt is not None:
                    if (
                        receipt.task_id != task_id
                        or receipt.request_hash != feedback_admission.request_hash
                    ):
                        raise FeedbackError(
                            409, "idempotency_conflict", "Task revision receipt changed"
                        )
                    current = await get_task(db_session, task_id)
                    if current is None:
                        raise FeedbackError(404, "not_found", "Task is unavailable")
                    await feedback_admission.participant(db_session, receipt, True)
                    await db_session.commit()
                    return TaskRevisionResult(
                        task=_row_to_task_model(current),
                        target_step=receipt.target_step,
                        superseded_count=receipt.superseded_count,
                        relaunched=False,
                        admitted_attempt_number=receipt.attempt_number,
                    )
                fence = await db_session.execute(
                    update(Task)
                    .where(
                        Task.task_id == task_id,
                        Task.feedback_state_generation == feedback_admission.expected_generation,
                        Task.attempt_number == expected_attempt,
                    )
                    .values(feedback_state_generation=Task.feedback_state_generation)
                )
                if affected(fence) != 1:
                    receipt = await db_session.get(
                        TaskRevisionReceiptRow, feedback_admission.revision_request_id
                    )
                    if (
                        receipt is not None
                        and receipt.request_hash == feedback_admission.request_hash
                    ):
                        current = await get_task(db_session, task_id)
                        if current is None:
                            raise FeedbackError(404, "not_found", "Task is unavailable")
                        await feedback_admission.participant(db_session, receipt, True)
                        await db_session.commit()
                        return TaskRevisionResult(
                            task=_row_to_task_model(current),
                            target_step=receipt.target_step,
                            superseded_count=receipt.superseded_count,
                            relaunched=False,
                            admitted_attempt_number=receipt.attempt_number,
                        )
                    raise FeedbackError(
                        409, "task_state_changed", "Task safety state changed; reconfirm"
                    )
            if expected_attempt is not None and not await assert_task_attempt_current(
                db_session, task_id, expected_attempt
            ):
                raise ValueError("Task attempt changed; refresh before mutating")
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            task = _row_to_task_model(task_row)
            if task.status == TaskStatus.CANCELLED:
                raise ValueError("Cancelled tasks cannot be revised")
            if task.workflow_id is None:
                raise ValueError("Task has no workflow to revise")
            workflow = self._pinned_workflow(task)
            if workflow is None:
                workflow = await self._workflow_registry.get(
                    task.workflow_id,
                    owner_email=task.created_by,
                    project_id=task.project_id,
                    include_disabled=True,
                )
            if workflow is None:
                raise ValueError("Workflow not found")
            step_names = [step.name for step in workflow.steps]
            if not step_names:
                raise ValueError("Workflow has no steps")
            if not target_step:
                raise ValueError("A target step is required for revision")
            if target_step not in step_names:
                raise ValueError("Target step not found")

            pending_pause = self._get_pending_interaction(task_id)
            state = task.workflow_state or WorkflowState()
            target_index = step_names.index(target_step)
            new_attempt_number = int(getattr(task_row, "attempt_number", 1) or 1) + 1
            state.current_step_index = target_index
            state.status = "running"
            state.current_step_status = None
            state.pending_pause_type = None
            state.pending_pause_payload = None
            state.last_operator_instruction = instruction
            state.last_revision_context = instruction
            for step_name in step_names[target_index:]:
                state.step_outputs.pop(step_name, None)
            superseded_count = await supersede_step_runs_for_revision(
                db_session,
                task_id,
                step_names[target_index:],
                before_attempt_number=new_attempt_number,
            )
            task_row.attempt_number = new_attempt_number
            task_row.status = "running"
            task_row.completed_at = None
            task_row.result_summary = None
            task_row.result_data = None
            task_row.workflow_state = state.model_dump(mode="json")
            task_row.updated_at = datetime.now(UTC)
            await self._execution_store.invalidate_task_lease(db_session, task_id)
            if feedback_admission is not None:
                await db_session.execute(
                    update(NotificationRow)
                    .where(
                        NotificationRow.task_id == task_id,
                        NotificationRow.status == "pending",
                    )
                    .values(
                        status="resolved",
                        resolution={
                            "decision": "revise",
                            "feedback_submission_id": feedback_admission.submission_id,
                        },
                        revision=NotificationRow.revision + 1,
                        resolved_at=datetime.now(UTC),
                    )
                )
                receipt = TaskRevisionReceiptRow(
                    revision_request_id=feedback_admission.revision_request_id,
                    task_id=task_id,
                    actor_email=task.created_by,
                    idempotency_key=feedback_admission.submission_id,
                    submission_id=feedback_admission.submission_id,
                    request_hash=feedback_admission.request_hash,
                    attempt_number=new_attempt_number,
                    target_step=step_names[target_index],
                    state="running",
                    superseded_count=superseded_count,
                )
                db_session.add(receipt)
                await db_session.flush()
                await feedback_admission.participant(db_session, receipt, False)
            await db_session.commit()
            await db_session.refresh(task_row)
            task = _row_to_task_model(task_row)

        if pending_pause is not None and feedback_admission is None:
            notification_service = self._workflow_engine._notification_service  # noqa: SLF001
            if notification_service is not None:
                await notification_service.resolve(
                    pending_pause.pause_id,
                    "revise",
                    {"note": instruction},
                    user_email=task.created_by,
                )
            else:
                self._workflow_engine._pause_waiter.resolve(  # noqa: SLF001
                    pending_pause.pause_id,
                    PauseResolution(decision="revise", data={"note": instruction}),
                )
        await self._cancel_active_run_for_revision(task_id)
        claim = await self._execution_store.claim_existing(
            task_id,
            statuses={"running"},
        )
        if claim is None:
            return TaskRevisionResult(
                task=task,
                target_step=step_names[target_index],
                superseded_count=superseded_count,
                relaunched=False,
            )
        self._launch_claimed_task_run(task, claim)
        return TaskRevisionResult(
            task=task,
            target_step=step_names[target_index],
            superseded_count=superseded_count,
            relaunched=True,
        )

    async def _cancel_active_run_for_revision(self, task_id: str) -> None:
        """Stop an existing paused run so a revision starts from refreshed DB state."""

        run_task = self._active_runs.get(task_id)
        if run_task is None or run_task.done():
            return
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    async def _clone_task_for_rerun(self, source_task: TaskModel) -> TaskModel:
        """Clone a terminal task into a fresh submitted task."""

        draft = await self.create_draft(
            created_by=source_task.created_by,
            creation_origin=source_task.creation_origin,
            agent_id=source_task.agent_id,
            title=source_task.title,
            description=source_task.description,
            expected_output=source_task.expected_output,
            priority=source_task.priority,
            created_by_agent_id=source_task.created_by_agent_id,
            delivery=source_task.delivery,
            completion_delivery=source_task.completion_delivery,
            workflow_id=source_task.workflow_id,
            project_id=source_task.project_id,
            workspace_root=source_task.workspace_root,
            working_directory=source_task.working_directory,
            source_type=source_task.source_type,
            source_ref=source_task.source_ref,
        )

        async with self._session_factory() as db_session:
            dependencies = await get_task_dependencies(db_session, source_task.task_id)
            for dependency in dependencies:
                await add_task_dependency(
                    db_session,
                    draft.task_id,
                    dependency.depends_on,
                    required=bool(dependency.required),
                )
            await db_session.commit()

        return await self.submit_existing(draft.task_id)

    async def cancel_task(
        self,
        task_id: str,
        *,
        expected_attempt: int | None = None,
    ) -> TaskModel:
        """Serialize cancellation with revisions and resume claims."""

        async with self._pick_lock:
            task, transitioned = await self._cancel_task_locked(
                task_id,
                expected_attempt=expected_attempt,
            )
        if transitioned:
            await self._workflow_engine.deliver_task_outcome(task)
        return task

    async def _cancel_task_locked(
        self,
        task_id: str,
        *,
        expected_attempt: int | None = None,
    ) -> tuple[TaskModel, bool]:
        """Cancel a task in any mutable state."""
        from cognis.store.models import Task as TaskRow

        pending_pause = self._get_pending_interaction(task_id)
        step_runs: list[Any] = []
        transitioned = False

        async with self._session_factory() as db_session:
            if expected_attempt is not None and not await assert_task_attempt_current(
                db_session, task_id, expected_attempt
            ):
                raise ValueError("Task attempt changed; refresh before mutating")
            task_row = await db_session.get(TaskRow, task_id, with_for_update=True)
            if task_row is None:
                raise ValueError("Task not found")
            if expected_attempt is not None and task_row.attempt_number != expected_attempt:
                raise ValueError("Task attempt changed; refresh before mutating")
            if task_row.status in {"completed", "failed"}:
                return _row_to_task_model(task_row), False
            step_runs = await list_step_runs_for_task(db_session, task_id)
            if task_row.status != "cancelled":
                ok = await update_task_status(
                    db_session,
                    task_id,
                    "cancelled",
                    completed_at=datetime.now(UTC),
                    expected_attempt=expected_attempt,
                )
                if not ok:
                    raise ValueError("Task could not be cancelled")
                transitioned = True
                await fail_running_step_runs_for_task(
                    db_session,
                    task_id,
                    datetime.now(UTC),
                    final_status="cancelled",
                )
                await self._execution_store.invalidate_task_lease(db_session, task_id)
            # Clear the durable interaction before fencing workers out of cleanup.
            # Also repair stale pauses on idempotent cancellation of legacy tasks.
            if task_row.workflow_state:
                state = dict(task_row.workflow_state)
                state.update(
                    status="cancelled",
                    current_step_status=None,
                    pending_pause_type=None,
                    pending_pause_payload=None,
                    version=int(state.get("version", 0)) + 1,
                )
                task_row.workflow_state = state
            await db_session.commit()
            task = _row_to_task_model(task_row)

        if pending_pause is not None:
            self._workflow_engine._pause_waiter.resolve(  # noqa: SLF001
                pending_pause.pause_id,
                self._build_cancel_resolution(pending_pause.pause_type),
            )

        notification_service = getattr(self._workflow_engine, "_notification_service", None)
        if notification_service is not None:
            with contextlib.suppress(Exception):
                await notification_service.mark_task_notifications_terminal(
                    task_id,
                    reason="task_cancelled",
                )

        control = self._run_controls.get(task_id)
        if control is not None:
            control.set()
        run_task = self._active_runs.get(task_id)
        if run_task is not None:
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(run_task), timeout=5.0)
        await self._cancel_task_owned_work(task, step_runs)
        if transitioned:
            cluster_signals = getattr(self._workflow_engine, "cluster_signals", None)
            if cluster_signals is not None:
                await cluster_signals.publish_task_change(task_id)
        return task.model_copy(update={"status": TaskStatus.CANCELLED}), transitioned

    async def _cancel_task_owned_work(self, task: TaskModel, step_runs: list[Any]) -> None:
        """Stop only sessions and managed turns proven to belong to task StepRuns."""

        self._cancel_dispatch_wakeup.set()
        agent_loop = getattr(self._workflow_engine, "_agent_loop", None)
        session_manager = getattr(self._workflow_engine, "_session_manager", None)
        for step_run in step_runs:
            if str(getattr(step_run, "status", "")) not in {
                "pending",
                "running",
                "evaluating",
                "paused",
                "cancelled",
            }:
                continue
            session_id = getattr(step_run, "session_id", None)
            if session_id and agent_loop is not None:
                with contextlib.suppress(Exception):
                    await agent_loop.cancel_children(session_id)
            if session_id:
                async with self._session_factory() as db_session:
                    links = await list_active_managed_conversation_links_for_controller_session(
                        db_session,
                        user_email=task.created_by,
                        controller_session_id=session_id,
                    )
                for link in links:
                    if self._turn_scheduler is not None:
                        with contextlib.suppress(Exception):
                            await self._turn_scheduler.cancel_turn(
                                link.target_conversation_id,
                                clear_queue=False,
                            )
                    async with self._session_factory() as db_session:
                        await update_managed_conversation_link(
                            db_session,
                            link.link_id,
                            conversation_state="closed",
                            turn_state="interrupted",
                            clear_active_turn_id=True,
                            last_error="Parent task cancelled",
                        )
                        await db_session.commit()
            conversation_id = getattr(step_run, "conversation_id", None)
            if conversation_id and self._turn_scheduler is not None:
                with contextlib.suppress(Exception):
                    await self._turn_scheduler.cancel_turn(conversation_id, clear_queue=False)
            if session_id and session_manager is not None:
                with contextlib.suppress(Exception):
                    await session_manager.mark_completed(
                        session_id,
                        completion_reason="task_cancelled",
                    )

    async def resolve_dependencies(self, completed_task_id: str) -> list[str]:
        """Re-evaluate dependents when a task completes.

        Returns list of task IDs that were transitioned to 'ready'.
        """
        transitioned: list[str] = []

        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            dependent_ids = await get_dependent_tasks(db_session, completed_task_id)

            for dep_id in dependent_ids:
                unmet = await get_unmet_dependencies(db_session, dep_id)
                if not unmet:
                    # All dependencies met — transition to ready
                    ok = await update_task_status(db_session, dep_id, "ready")
                    if ok:
                        transitioned.append(dep_id)
                        TASKS_TOTAL.labels(status="ready").inc()
                else:
                    # Check for failed required dependencies
                    failed_required = [d for d in unmet if d.required]
                    if failed_required:
                        # Check if the depended-on task actually failed
                        for dep in failed_required:
                            dep_task = await get_task(db_session, dep.depends_on)
                            if dep_task and dep_task.status in ("failed", "cancelled"):
                                # Flag for user decision — pause the task
                                await update_task_status(db_session, dep_id, "paused")
                                break

            await db_session.commit()

        # Wake the drain loop for newly ready tasks
        if transitioned:
            await self._wake_workers()

        return transitioned

    async def _cancelled_dispatch_loop(self) -> None:
        """Retry terminal cancellation outside workflow claiming/capacity."""
        from cognis.core.invariants.task_dispatches import reconcile_cancelled_dispatches

        while not self._stop_event.is_set():
            self._cancel_dispatch_wakeup.clear()
            try:
                await reconcile_cancelled_dispatches(
                    self._session_factory, self._websocket_provider
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Task dispatch cancellation reconciliation failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._cancel_dispatch_wakeup.wait(), timeout=30.0)

    async def recover_stale_tasks(
        self, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    ) -> list[str]:
        """Take over running tasks only after their database lease expires."""
        del stale_after_seconds
        recovered: list[str] = []
        async with self._session_factory() as db_session:
            stale_tasks = await list_tasks_by_status(db_session, ["running"], limit=1000)

        for row in stale_tasks:
            local_run = self._active_runs.get(row.task_id)
            if local_run is not None and not local_run.done():
                continue
            claim = await self._execution_store.claim_existing(
                row.task_id, statuses={"running"}, capacity=False
            )
            if claim is None:
                continue
            task = _row_to_task_model(row)
            fence = TaskExecutionFence(self._execution_store, claim, asyncio.Event())
            fence.start()
            try:
                classification, details = await self._classify_crash_recovery(task)
                TASK_RECOVERY_CLASSIFICATIONS.labels(classification=classification).inc()
                async with self._session_factory() as db_session:
                    await fence.assert_current(db_session)
                    if classification == "ambiguous_tool_execution":
                        await fail_running_step_runs_for_task(
                            db_session, task.task_id, datetime.now(UTC)
                        )
                        await update_task_status(
                            db_session,
                            task.task_id,
                            "failed",
                            completed_at=datetime.now(UTC),
                            result_summary=(
                                "Workflow recovery stopped because a tool call may have "
                                "completed without a durable result; it was not replayed."
                            ),
                            result_data={
                                **(task.result_data or {}),
                                "recovery_classification": classification,
                                **details,
                            },
                        )
                    else:
                        await defer_running_task(
                            db_session,
                            task.task_id,
                            scheduled_for=datetime.now(UTC)
                            + timedelta(
                                seconds=30 if classification == "infrastructure_wait" else 0
                            ),
                            result_summary="Workflow is recovering interrupted execution.",
                        )
                    await db_session.commit()
                if classification == "ambiguous_tool_execution":
                    await self._event_bus.publish(
                        Event(type=EventType.TASK_FAILED, data={"task_id": task.task_id})
                    )
                    task.status = TaskStatus.FAILED
                    await self._deliver_terminal_task_failure(task)
            finally:
                await fence.close()
            await self._wake_workers()
            recovered.append(task.task_id)
            logger.info(
                "Recovered stale task",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "classification": classification,
                    }
                },
            )

        return recovered

    async def _classify_crash_recovery(self, task: TaskModel) -> tuple[str, dict[str, Any]]:
        async with self._session_factory() as db_session:
            step_runs = await list_step_runs_for_task(db_session, task.task_id)
        batches = (task.result_data or {}).get("execution_recovery") or {}
        if batches:
            return "recoverable_pending_tool_calls", {}
        running = next(
            (row for row in reversed(step_runs) if row.status in {"running", "pending", "failed"}),
            None,
        )
        if running is None:
            return "safe_before_tool_dispatch", {}
        runtime_info = dict(running.runtime_info) if isinstance(running.runtime_info, dict) else {}
        if runtime_info.get("deterministic_step") is True:
            substate = runtime_info.get("deterministic_substate")
            details = {
                "step_run_id": running.step_run_id,
                "step_type": running.step_type,
                "substate": substate,
                "tool_name": runtime_info.get("tool_name"),
                "call_identity": runtime_info.get("call_identity"),
            }
            if substate == "persisted" and isinstance(running.output, dict):
                return "recoverable_persisted_deterministic_output", details
            if substate in {None, "rendering"}:
                return "safe_before_tool_dispatch", details
            if substate == "executing" and runtime_info.get("tool_read_only") is True:
                return "retry_read_only_deterministic_tool", details
            if substate == "executing" and runtime_info.get("durable_tool_dispatch") is True:
                return "recoverable_pending_tool_calls", details
            return "ambiguous_tool_execution", {
                **details,
                "reason": "ambiguous_deterministic_tool_dispatch",
            }
        if not running.intaris_session_id:
            return "safe_before_tool_dispatch", {"step_run_id": running.step_run_id}

        guardrails = getattr(self._workflow_engine, "_providers", None)
        guardrails = getattr(guardrails, "guardrails", None)
        if guardrails is None or not hasattr(guardrails, "read_events"):
            return "infrastructure_wait", {
                "step_run_id": running.step_run_id,
                "reason": "tool_event_history_unavailable",
            }
        try:
            events: list[dict[str, Any]] = []
            after_seq = 0
            while True:
                event_read = await guardrails.read_events(
                    session_id=running.intaris_session_id,
                    after_seq=after_seq,
                    limit=500,
                    types=["tool_call", "tool_result"],
                    allow_missing_stream=True,
                )
                if getattr(event_read, "missing_stream_fallback_used", False):
                    raise RuntimeError("Intaris event history used a missing-stream fallback")
                events.extend(event for event in event_read.events if isinstance(event, dict))
                next_after_seq = next_event_page_after_seq(event_read, after_seq)
                if next_after_seq is None:
                    break
                after_seq = next_after_seq
        except Exception:
            logger.exception(
                "task_queue: failed to read tool events during crash recovery",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            return "infrastructure_wait", {
                "step_run_id": running.step_run_id,
                "reason": "tool_event_history_unavailable",
            }

        calls: dict[str, str | None] = {}
        results: set[str] = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            raw_data = event.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            call_id = data.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if event.get("type") == "tool_call":
                calls[call_id] = data.get("name") or data.get("tool_name")
            elif event.get("type") == "tool_result":
                results.add(call_id)
        unmatched = [call_id for call_id in calls if call_id not in results]
        if unmatched:
            first = unmatched[0]
            return "ambiguous_tool_execution", {
                "step_run_id": running.step_run_id,
                "call_id": first,
                "tool_name": calls[first],
            }
        if calls:
            return "recoverable_completed_tool_calls", {"step_run_id": running.step_run_id}
        return "safe_before_tool_dispatch", {"step_run_id": running.step_run_id}

    async def recover_paused_tasks(self) -> list[str]:
        """Re-enter paused workflows so prompts and waits are recreated after restart.

        Each pause type has a distinct recovery contract:

        * ``gate`` — the workflow engine re-creates the gate notification
          when the task resumes.
        * ``step_input`` — if the user already responded before the
          restart, the payload carries ``response`` and the agent loop
          consumes it through ``_get_recovered_step_response``; relaunch
          without registering a new PauseWaiter (a fresh waiter would
          never be resolved and would pollute ``find_pending()`` output).
          If no response is persisted, register a waiter and keep the
          task paused until the user answers through the API.
        * ``credential_request`` / ``auth_challenge`` — resolutions for
          these pauses are stored on the ``NotificationRow`` (not on the
          task workflow state). If the notification is already
          ``resolved``, register the waiter and immediately replay the
          resolution so the relaunched agent loop can consume the
          response without blocking. Otherwise keep the task paused.

        Without relaunching, tasks whose awaiting coroutine died with
        the controller would stay paused forever.
        """

        from cognis.core.agent_loop import PauseResolution, PendingPause  # noqa: F401

        recovered: list[str] = []
        async with self._session_factory() as db_session:
            paused_rows = await list_tasks_by_status(db_session, ["paused"], limit=1000)

        for row in paused_rows:
            task = _row_to_task_model(row)
            if task.workflow_state is None:
                continue
            pause_type = task.workflow_state.pending_pause_type
            if pause_type is None:
                continue

            payload = task.workflow_state.pending_pause_payload or {}
            pause_id = str(payload.get("pause_id", f"recovered_{task.task_id}"))

            if pause_type == "gate":
                await self._launch_recovered_paused_task(task)
            elif pause_type == "step_input":
                await self._recover_step_input_pause(task=task, pause_id=pause_id, payload=payload)
            elif pause_type in {"credential_request", "auth_challenge"}:
                await self._recover_credential_or_auth_pause(
                    task=task,
                    pause_id=pause_id,
                    pause_type=str(pause_type),
                    payload=payload,
                )
            else:
                logger.warning(
                    "task_queue: unknown pause type during recovery",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "pause_type": pause_type,
                        }
                    },
                )
            recovered.append(task.task_id)

        return recovered

    async def _recover_step_input_pause(
        self,
        *,
        task: TaskModel,
        pause_id: str,
        payload: dict[str, Any],
    ) -> None:
        from cognis.core.agent_loop import PendingPause

        if payload.get("answers") is not None:
            # The HTTP respond path persisted the user's answer before
            # the restart; the relaunched agent loop consumes it via
            # ``_get_recovered_step_response`` without a PauseWaiter.
            await self._launch_recovered_paused_task(task)
            return

        existing = self._workflow_engine._pause_waiter.get(pause_id)  # noqa: SLF001
        if existing is None:
            self._workflow_engine._pause_waiter.register(  # noqa: SLF001
                PendingPause(
                    pause_id=pause_id,
                    pause_type="step_input",
                    task_id=task.task_id,
                    step_name=payload.get("step_name"),
                    step_run_id=payload.get("step_run_id"),
                    session_id=payload.get("session_id"),
                    questions=payload.get("questions")
                    if isinstance(payload.get("questions"), list)
                    else None,
                    context=_recover_pause_context(payload.get("context")),
                )
            )
        await self._launch_recovered_paused_task(task)

    async def _recover_credential_or_auth_pause(
        self,
        *,
        task: TaskModel,
        pause_id: str,
        pause_type: str,
        payload: dict[str, Any],
    ) -> None:
        from cognis.core.agent_loop import PauseResolution, PendingPause

        resolved_decision: str | None = None
        resolved_data: dict[str, Any] | None = None

        notification_service = getattr(self._workflow_engine, "_notification_service", None)
        if notification_service is not None:
            try:
                notif_row = await notification_service.get(pause_id)
            except Exception:
                notif_row = None
            if notif_row is not None and getattr(notif_row, "status", None) == "resolved":
                resolution = getattr(notif_row, "resolution", None)
                if isinstance(resolution, dict):
                    resolved_decision = str(resolution.get("decision") or "continue")
                    resolved_data = {
                        key: value
                        for key, value in resolution.items()
                        if key not in {"decision", "state"}
                    }

        if resolved_decision is not None and resolved_data is not None:
            self._workflow_engine._pause_waiter.register(  # noqa: SLF001
                PendingPause(
                    pause_id=pause_id,
                    pause_type=pause_type,
                    task_id=task.task_id,
                    step_name=payload.get("step_name"),
                    step_run_id=payload.get("step_run_id"),
                    session_id=payload.get("session_id"),
                    question=payload.get("question") or payload.get("message"),
                    options=_recover_pause_options(payload.get("options")),
                    context=_recover_pause_context(payload.get("context")),
                )
            )
            self._workflow_engine._pause_waiter.resolve(  # noqa: SLF001
                pause_id,
                PauseResolution(decision=resolved_decision, data=resolved_data),
            )
            logger.info(
                "task_queue: replaying resolved credential/auth notification on recovery",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "pause_type": pause_type,
                        "decision": resolved_decision,
                    }
                },
            )
            await self._launch_recovered_paused_task(task)
            return

        existing = self._workflow_engine._pause_waiter.get(pause_id)  # noqa: SLF001
        if existing is None:
            self._workflow_engine._pause_waiter.register(  # noqa: SLF001
                PendingPause(
                    pause_id=pause_id,
                    pause_type=pause_type,
                    task_id=task.task_id,
                    step_name=payload.get("step_name"),
                    step_run_id=payload.get("step_run_id"),
                    session_id=payload.get("session_id"),
                    question=payload.get("question") or payload.get("message"),
                    options=_recover_pause_options(payload.get("options")),
                    context=_recover_pause_context(payload.get("context")),
                )
            )
        await self._launch_recovered_paused_task(task)

    async def recover_orphaned_running_step_runs(self) -> int:
        """Finalize running step runs whose parent tasks are already terminal."""

        async with self._session_factory() as db_session:
            recovered = await fail_orphaned_running_step_runs(
                db_session,
                datetime.now(UTC),
                final_status="failed",
            )
            await db_session.commit()
        if recovered:
            logger.info(
                "Recovered orphaned running step runs",
                extra={"extra_data": {"count": recovered}},
            )
        return recovered

    async def _drain_loop(self) -> None:
        """Main queue processing loop."""
        next_scheduled: float | None = None
        while not self._stop_event.is_set():
            try:
                # Wait for wake signal or poll interval
                timeout = self._poll_interval
                if next_scheduled is not None:
                    timeout = min(
                        timeout,
                        max(0.0, next_scheduled - asyncio.get_running_loop().time()),
                    )
                reason = "signal"
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    reason = "fallback"
                self._wake_event.clear()

                if self._stop_event.is_set():
                    break
                QUEUE_RECONCILIATIONS.labels(reason=reason).inc()

                # Clean up completed runs
                for task_id in list(self._active_runs):
                    if self._active_runs[task_id].done():
                        self._active_runs.pop(task_id)
                        self._run_controls.pop(task_id, None)

                loop_time = asyncio.get_running_loop().time()
                if loop_time >= self._next_recovery_at:
                    self._next_recovery_at = loop_time + 30.0
                    await self.recover_stale_tasks()
                    await self.recover_paused_tasks()

                # Try to pick and run a task
                # Read the next deadline before claiming so a deadline crossed
                # during this read is covered by the following durable claim.
                deadline_base = asyncio.get_running_loop().time()
                scheduled_delay = await self._execution_store.next_scheduled_delay()
                next_scheduled = (
                    deadline_base + scheduled_delay if scheduled_delay is not None else None
                )
                await self._try_pick_and_run()

            except Exception:
                logger.exception("Task queue drain loop error")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake_event.wait(), timeout=min(1.0, self._poll_interval)
                    )
                # Retry directly rather than adding another idle fallback wait.
                next_scheduled = None
                self._wake_event.set()

    async def _try_pick_and_run(self) -> None:
        """Try to pick the next ready task and start executing it."""
        async with self._pick_lock:
            QUEUE_CLAIM_ATTEMPTS.inc()
            claim = await self._execution_store.claim_ready()
        if claim is None:
            return
        try:
            async with self._session_factory() as db_session:
                row = await get_task(db_session, claim.task_id)
                task = _row_to_task_model(row) if row is not None else None
        except Exception:
            await self._execution_store.release(claim)
            raise
        if task is None:
            await self._execution_store.release(claim)
            logger.warning(
                "Task disappeared after execution claim",
                extra={"extra_data": {"task_id": claim.task_id}},
            )
            return

        TASKS_TOTAL.labels(status="running").inc()

        await self._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                data={"task_id": task.task_id},
            )
        )
        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is not None:
            await cluster_signals.publish_task_change(task.task_id)

        # Start workflow execution in background
        self._launch_claimed_task_run(task, claim)
        # Signals coalesce: drain until durable work or capacity is exhausted.
        self._wake_event.set()

    async def _run_task(self, task: TaskModel, claim: TaskExecutionClaim) -> None:
        """Execute a task's workflow.

        Runs inside ``scoped_runtime_context`` so that all downstream
        Intaris calls (event recording, evaluation, delivery) use the
        correct user identity instead of the default ``system@example.com``.
        """
        cancel_event = self._run_controls.setdefault(task.task_id, asyncio.Event())
        fence = TaskExecutionFence(self._execution_store, claim, cancel_event)
        fence.start()
        fence_token = bind_task_execution_fence(fence)
        try:
            agent = await self._agent_registry.get(task.agent_id, owner_email=task.created_by)
        except BaseException:
            reset_task_execution_fence(fence_token)
            await fence.close()
            self._active_runs.pop(task.task_id, None)
            self._run_controls.pop(task.task_id, None)
            raise
        with scoped_runtime_context(
            user_email=task.created_by,
            agent_id=task.agent_id,
            agent_owner_email=(agent.owner_email if agent is not None else task.created_by),
            workspace_root=task.workspace_root,
            effective_working_directory=task.working_directory or task.workspace_root,
        ):
            try:
                workflow_id = task.workflow_id or "system:general-task"
                state_was_absent = task.workflow_state is None
                workflow = self._pinned_workflow(task)
                if workflow is None:
                    workflow = await self._workflow_registry.get(
                        workflow_id, owner_email=task.created_by, project_id=task.project_id
                    )
                if workflow is None:
                    result_summary = f"Unknown workflow: {workflow_id}"
                    logger.warning(
                        "Unknown workflow for task",
                        extra={"extra_data": {"task_id": task.task_id, "workflow_id": workflow_id}},
                    )
                    fence.mark_terminal(str(TaskStatus.FAILED))
                    async with self._session_factory() as db_session:
                        await assert_task_execution_fence(db_session)
                        await update_task_status(
                            db_session,
                            task.task_id,
                            "failed",
                            result_summary=result_summary,
                            completed_at=datetime.now(UTC),
                            expected_attempt=task.attempt_number,
                        )
                        await db_session.commit()
                    task.status = TaskStatus.FAILED
                    task.completed_at = datetime.now(UTC)
                    task.result_summary = result_summary
                    await self._deliver_terminal_task_failure(task)
                    return

                # Initialize workflow state. Only fresh tasks are pinned here:
                # persisted in-flight tasks without a snapshot retain legacy
                # current-definition lookup behavior.
                task.workflow_state = task.workflow_state or WorkflowState()
                if state_was_absent:
                    pin_effective_workflow(task.workflow_state, workflow)
                async with self._session_factory() as db_session:
                    await assert_task_execution_fence(db_session)
                    await update_task_workflow_state(
                        db_session,
                        task.task_id,
                        task.workflow_state.model_dump(mode="json"),
                    )
                    await db_session.commit()

                # Execute
                result = await self._workflow_engine.execute_workflow(
                    task,
                    workflow,
                    cancel_event=cancel_event,
                )

                # Resolve dependencies — every terminal transition may
                # unblock or pause dependent tasks, not just completion.
                if result.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    if str(getattr(workflow, "lifecycle", "persistent")) == "ephemeral":
                        async with self._session_factory() as db_session:
                            await assert_task_execution_fence(db_session)
                            await update_workflow(
                                db_session,
                                workflow.workflow_id,
                                updates={"archived_at": datetime.now(UTC)},
                            )
                            await db_session.commit()
                    await self.resolve_dependencies(result.task_id)

            except StaleTaskExecutionOwner:
                logger.warning(
                    "Task execution lost ownership",
                    extra={"extra_data": {"task_id": task.task_id}},
                )
            except asyncio.CancelledError:
                logger.info(
                    "Task execution cancelled",
                    extra={"extra_data": {"task_id": task.task_id}},
                )
                async with self._session_factory() as db_session:
                    try:
                        await assert_task_execution_fence(db_session)
                    except StaleTaskExecutionOwner:
                        return
                    task_row = await get_task(db_session, task.task_id)
                    current_status = str(task_row.status) if task_row is not None else "failed"
                    if current_status == "running":
                        # Shutdown/lease recovery is not user cancellation.
                        # Leave the step's in-flight evidence intact.
                        await db_session.rollback()
                        return
                    step_run_status = {
                        "cancelled": "cancelled",
                        "paused": "paused",
                        "queued": "failed",
                        "running": "failed",
                    }.get(current_status, "failed")
                    await fail_running_step_runs_for_task(
                        db_session,
                        task.task_id,
                        datetime.now(UTC),
                        final_status=step_run_status,
                    )
                    await db_session.commit()
            except Exception as exc:
                logger.exception(
                    "Task execution failed",
                    extra={"extra_data": {"task_id": task.task_id}},
                )
                result_summary = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                completed_at = datetime.now(UTC)
                fence.mark_terminal(str(TaskStatus.FAILED))
                async with self._session_factory() as db_session:
                    await assert_task_execution_fence(db_session)
                    await update_task_status(
                        db_session,
                        task.task_id,
                        "failed",
                        result_summary=result_summary,
                        completed_at=completed_at,
                        expected_attempt=task.attempt_number,
                    )
                    await db_session.commit()
                task.status = TaskStatus.FAILED
                task.completed_at = completed_at
                task.result_summary = result_summary
                await self._deliver_terminal_task_failure(task)
            finally:
                reset_task_execution_fence(fence_token)
                await fence.close()
                await self._wake_workers()
                self._active_runs.pop(task.task_id, None)
                self._run_controls.pop(task.task_id, None)
                # Update queue depth metric
                async with self._session_factory() as db_session:
                    queued = await list_tasks_by_status(db_session, ["queued", "ready"])
                QUEUE_DEPTH.labels(queue="default").set(len(queued))

    @staticmethod
    def _pinned_workflow(task: TaskModel) -> Workflow | None:
        """Return the persisted effective definition for this task attempt."""

        state = task.workflow_state
        if state is None or state.effective_workflow_definition is None:
            return None
        return Workflow.model_validate(state.effective_workflow_definition)

    async def _deliver_terminal_task_failure(self, task: TaskModel) -> None:
        """Deliver failures that happen before WorkflowEngine owns completion."""

        try:
            await self._workflow_engine._deliver_task_result(task)  # noqa: SLF001
        except Exception:
            logger.warning(
                "Task failure delivery failed",
                extra={"extra_data": {"task_id": task.task_id}},
                exc_info=True,
            )

    async def _has_capacity(self, agent_id: str | None = None) -> bool:
        """Check if there's capacity to run another step.

        Enforces both global and per-agent limits.
        """
        # Informational compatibility helper. Capacity is authoritatively
        # acquired with the task claim in TaskExecutionStore.
        return True

    async def _try_transition_to_ready(self, task_id: str) -> bool:
        """Try to transition a queued task to ready if all deps are met."""
        async with self._session_factory() as db_session:
            unmet = await get_unmet_dependencies(db_session, task_id)
            if not unmet:
                ok = await update_task_status(db_session, task_id, "ready")
                await db_session.commit()
                if ok:
                    TASKS_TOTAL.labels(status="ready").inc()
            else:
                ok = False
        if ok:
            await self._wake_workers()
        return bool(ok)

    async def _launch_recovered_paused_task(self, task: TaskModel) -> None:
        active = self._active_runs.get(task.task_id)
        if active is not None and not active.done():
            return
        claim = await self._execution_store.claim_paused(task.task_id)
        if claim is not None:
            self._launch_claimed_task_run(task, claim)

    def _launch_claimed_task_run(self, task: TaskModel, claim: TaskExecutionClaim) -> None:
        active = self._active_runs.get(task.task_id)
        if active is not None and not active.done():
            asyncio.create_task(self._execution_store.release(claim))
            return
        self._task_claims[task.task_id] = claim
        self._launch_task_run(task)

    def _launch_task_run(self, task: TaskModel) -> None:
        """Start or restart a task execution coroutine."""
        if task.task_id in self._active_runs and not self._active_runs[task.task_id].done():
            return
        claim = self._task_claims.pop(task.task_id)
        self._run_controls[task.task_id] = asyncio.Event()
        run_task = asyncio.create_task(self._run_task(task, claim))
        self._active_runs[task.task_id] = run_task

    def _get_pending_interaction(self, task_id: str) -> Any:
        """Return a pending gate or step-input pause for a task."""
        pause_waiter = self._workflow_engine._pause_waiter  # noqa: SLF001
        return pause_waiter.find_pending(task_id=task_id)

    def _build_cancel_resolution(self, pause_type: str) -> PauseResolution:  # noqa: ARG002
        """Build a cancellation resolution for a pending pause.

        ``pause_type`` is accepted for future differentiation (e.g. gates
        vs step-input requests may warrant different resolution payloads).
        """
        return PauseResolution(decision="cancel", data={"response": ""})

    @property
    def active_run_count(self) -> int:
        """Return the number of actively executing tasks."""
        return len(self._active_runs)

    def has_active_run(self, task_id: str) -> bool:
        """Return True when the task currently has a running coroutine."""
        run_task = self._active_runs.get(task_id)
        return run_task is not None and not run_task.done()


def _row_to_task_model(row: Any) -> TaskModel:
    """Convert a DB Task row to a TaskModel."""
    return TaskModel(
        task_id=row.task_id,
        title=row.title,
        description=row.description or "",
        expected_output=getattr(row, "expected_output", None),
        status=TaskStatus(row.status),
        priority=row.priority,
        created_by=row.created_by,
        agent_id=row.agent_id,
        agent_profile_id=getattr(row, "agent_profile_id", None),
        created_by_agent_id=getattr(row, "created_by_agent_id", None),
        creation_origin=getattr(row, "creation_origin", None),
        source_type=row.source_type,
        source_ref=row.source_ref,
        source_session_id=getattr(row, "source_session_id", None),
        delivery=TaskDelivery(
            mode=row.delivery_mode,
            target=row.delivery_target,
        ),
        completion_delivery=CompletionDeliveryPolicy(
            completion_mode_family=getattr(row, "completion_mode_family", "default"),
            allow_silent_completion=bool(getattr(row, "allow_silent_completion", False)),
            on_finish_instructions=getattr(row, "on_finish_instructions", None),
        ),
        interaction_mode_override=getattr(row, "interaction_mode_override", None),
        escalation_timeout_seconds=getattr(row, "escalation_timeout_seconds", None),
        session_policy=SessionPolicy.model_validate(getattr(row, "session_policy", None) or {}),
        workflow_id=row.workflow_id,
        project_id=getattr(row, "project_id", None),
        attempt_number=getattr(row, "attempt_number", 1),
        workspace_root=getattr(row, "workspace_root", None),
        working_directory=getattr(row, "working_directory", None),
        active_executor_id=getattr(row, "active_executor_id", None),
        active_executor_assigned_at=getattr(row, "active_executor_assigned_at", None),
        active_executor_expires_at=getattr(row, "active_executor_expires_at", None),
        active_executor_source=getattr(row, "active_executor_source", None),
        workflow_state=(
            WorkflowState.model_validate(row.workflow_state) if row.workflow_state else None
        ),
        queue_name=row.queue_name,
        scheduled_for=row.scheduled_for,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        result_summary=row.result_summary,
        result_data=row.result_data,
        applied_completion_mode=getattr(row, "applied_completion_mode", None),
        applied_completion_reason=getattr(row, "applied_completion_reason", None),
    )
