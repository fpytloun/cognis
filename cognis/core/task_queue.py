"""Task queue — priority-based task picking, dependency resolution, and lifecycle.

Uses polling for MVP (event-based wakeups can be added via asyncio.Event
signaling or Postgres LISTEN/NOTIFY in Phase 2). Capacity enforcement
uses a unified model: max active steps globally and per-agent.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import PauseResolution, PendingPause
from cognis.core.events import Event, EventBus, EventType
from cognis.core.workflow_engine import WorkflowEngine
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.logging import get_logger
from cognis.models.task import TaskDelivery, TaskModel, TaskStatus
from cognis.models.workflow import WorkflowState
from cognis.runtime_context import scoped_runtime_context
from cognis.store.queries import (
    count_active_steps,
    create_task,
    fail_running_step_runs_for_task,
    get_dependent_tasks,
    get_setting_value,
    get_task,
    get_unmet_dependencies,
    list_stale_running_tasks,
    list_tasks_by_status,
    pick_ready_task,
    update_task_status,
    update_task_workflow_state,
)

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

# Default capacity limits
DEFAULT_MAX_ACTIVE_STEPS_GLOBAL = 10
DEFAULT_MAX_ACTIVE_STEPS_PER_AGENT = 3
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_STALE_AFTER_SECONDS = 300


class TaskQueue:
    """Priority-based task queue with dependency resolution."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_engine: WorkflowEngine,
        workflow_registry: WorkflowRegistry,
        event_bus: EventBus,
        llm_provider: Any = None,
        max_active_steps_global: int = DEFAULT_MAX_ACTIVE_STEPS_GLOBAL,
        max_active_steps_per_agent: int = DEFAULT_MAX_ACTIVE_STEPS_PER_AGENT,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._workflow_engine = workflow_engine
        self._workflow_registry = workflow_registry
        self._event_bus = event_bus
        self._llm_provider = llm_provider
        self._max_active_global = max_active_steps_global
        self._max_active_per_agent = max_active_steps_per_agent
        self._poll_interval = poll_interval_seconds
        self._accepting = True
        self._stop_event = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None
        self._active_runs: dict[str, asyncio.Task[Any]] = {}
        self._run_controls: dict[str, asyncio.Event] = {}
        self._pick_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()

    @classmethod
    async def from_session_factory(
        cls,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        workflow_engine: WorkflowEngine,
        workflow_registry: WorkflowRegistry,
        event_bus: EventBus,
        llm_provider: Any = None,
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
            llm_provider=llm_provider,
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
        self._drain_task = asyncio.create_task(self._drain_loop())
        logger.info("Task queue started")

    async def stop(self) -> None:
        """Graceful shutdown.

        1. Stop accepting new tasks
        2. Signal active workflow runs to finish current LLM call
        3. Wait up to 30s for active steps to finalize
        4. Mark remaining running tasks as 'queued' for recovery
        5. Cancel the drain loop
        """
        self._accepting = False
        self._stop_event.set()
        self._wake_event.set()

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
            active_task_ids = list(self._active_runs)
            for task_id, run_task in list(self._active_runs.items()):
                control = self._run_controls.get(task_id)
                if control is not None:
                    control.set()
                run_task.cancel()

            await asyncio.gather(
                *self._active_runs.values(),
                return_exceptions=True,
            )

            # Re-queue tasks that were running
            async with self._session_factory() as db_session:
                for task_id in active_task_ids:
                    await update_task_status(db_session, task_id, "queued")
                await db_session.commit()

        self._active_runs.clear()
        self._run_controls.clear()
        logger.info("Task queue stopped")

    async def submit(
        self,
        *,
        created_by: str,
        agent_id: str,
        title: str,
        description: str = "",
        expected_output: str | None = None,
        priority: int = 0,
        source_type: str = "api",
        source_ref: str | None = None,
        delivery: TaskDelivery | None = None,
        workflow_id: str | None = None,
        scheduled_for: datetime | None = None,
        status: str = "queued",
    ) -> TaskModel:
        """Submit a task for execution.

        Creates the task in the DB and triggers dependency resolution.
        """
        if not self._accepting:
            raise RuntimeError("Task queue is not accepting new tasks")

        delivery = delivery or TaskDelivery()

        async with self._session_factory() as db_session:
            row = await create_task(
                db_session,
                created_by=created_by,
                agent_id=agent_id,
                title=title,
                description=description,
                expected_output=expected_output,
                status=status,
                priority=priority,
                source_type=source_type,
                source_ref=source_ref,
                delivery_mode=delivery.mode,
                delivery_target=delivery.target,
                workflow_id=workflow_id,
                scheduled_for=scheduled_for,
            )
            await db_session.commit()

            task = _row_to_task_model(row)

        TASKS_TOTAL.labels(status=status).inc()
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_CREATED,
                data={"task_id": task.task_id, "status": status},
            )
        )

        # If queued, try to transition to ready
        if status == "queued":
            await self._try_transition_to_ready(task.task_id)

        # Wake the drain loop
        self._wake_event.set()

        return task

    async def create_draft(
        self,
        *,
        created_by: str,
        agent_id: str,
        title: str,
        description: str = "",
        expected_output: str | None = None,
        priority: int = 0,
        delivery: TaskDelivery | None = None,
        workflow_id: str | None = None,
        source_type: str = "api",
        source_ref: str | None = None,
    ) -> TaskModel:
        """Create a draft task visible in the kanban board."""
        return await self.submit(
            created_by=created_by,
            agent_id=agent_id,
            title=title,
            description=description,
            expected_output=expected_output,
            priority=priority,
            delivery=delivery,
            workflow_id=workflow_id,
            source_type=source_type,
            source_ref=source_ref,
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

    async def pause_task(self, task_id: str) -> TaskModel:
        """Pause a running task cooperatively."""
        async with self._session_factory() as db_session:
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

    async def resume_task(self, task_id: str) -> TaskModel:
        """Resume a paused task when capacity is available."""
        if self._get_pending_interaction(task_id) is not None:
            raise ValueError("Task is waiting for gate or step input response")

        async with self._session_factory() as db_session:
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            if task_row.status != "paused":
                raise ValueError("Only paused tasks can be resumed")
            task = _row_to_task_model(task_row)

        if not await self._has_capacity(agent_id=task.agent_id):
            raise ValueError("No execution capacity available to resume the task")

        async with self._session_factory() as db_session:
            ok = await update_task_status(db_session, task_id, "running")
            if not ok:
                raise ValueError("Task could not be resumed")
            await db_session.commit()

        task.status = TaskStatus.RUNNING
        self._launch_task_run(task)
        return task

    async def retry_failed_task(self, task_id: str) -> TaskModel:
        """Reset a failed task's workflow state and re-launch it.

        Unlike ``resume_task`` (which only works for paused tasks), this
        method handles tasks in ``failed`` status by resetting the attempt
        counter for the current step and transitioning back to ``running``.
        """
        async with self._session_factory() as db_session:
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            if task_row.status != "failed":
                raise ValueError("Only failed tasks can be retried via retry_failed_task")

            # Reset workflow state — clear attempt counters so the step
            # gets fresh attempts, and set status back to running.
            ws = (
                WorkflowState.model_validate(task_row.workflow_state)
                if task_row.workflow_state
                else WorkflowState()
            )
            ws.loop_iterations = {}  # Reset all attempt counters
            ws.status = "running"
            ws.current_step_status = None
            ws.pending_pause_type = None
            ws.pending_pause_payload = None

            task_row.workflow_state = ws.model_dump(mode="json")
            await update_task_status(db_session, task_id, "running")
            await db_session.commit()

            task = _row_to_task_model(task_row)

        task.workflow_state = ws
        self._launch_task_run(task)
        return task

    async def cancel_task(self, task_id: str) -> TaskModel:
        """Cancel a task in any mutable state."""
        pending_pause = self._get_pending_interaction(task_id)
        if pending_pause is not None:
            self._workflow_engine._pause_waiter.resolve(  # noqa: SLF001
                pending_pause.pause_id,
                self._build_cancel_resolution(pending_pause.pause_type),
            )

        async with self._session_factory() as db_session:
            task_row = await get_task(db_session, task_id)
            if task_row is None:
                raise ValueError("Task not found")
            if task_row.status in {"completed", "failed", "cancelled"}:
                return _row_to_task_model(task_row)
            ok = await update_task_status(
                db_session,
                task_id,
                "cancelled",
                completed_at=datetime.now(UTC),
            )
            if not ok:
                raise ValueError("Task could not be cancelled")
            if task_row.status == "paused":
                await fail_running_step_runs_for_task(
                    db_session,
                    task_id,
                    datetime.now(UTC),
                    final_status="cancelled",
                )
            await db_session.commit()
            task = _row_to_task_model(task_row)

        control = self._run_controls.get(task_id)
        if control is not None:
            control.set()
        run_task = self._active_runs.get(task_id)
        if run_task is not None:
            run_task.cancel()
        return task.model_copy(update={"status": TaskStatus.CANCELLED})

    async def resolve_dependencies(self, completed_task_id: str) -> list[str]:
        """Re-evaluate dependents when a task completes.

        Returns list of task IDs that were transitioned to 'ready'.
        """
        transitioned: list[str] = []

        async with self._session_factory() as db_session:
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
            self._wake_event.set()

        return transitioned

    async def recover_stale_tasks(
        self, stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    ) -> list[str]:
        """Recover tasks stuck in running state after controller restart.

        Called on startup after SessionManager.recover_stale_sessions().
        """
        threshold = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        recovered: list[str] = []

        async with self._session_factory() as db_session:
            stale_tasks = await list_stale_running_tasks(db_session, threshold)

            for task in stale_tasks:
                # Fail associated running step_runs
                await fail_running_step_runs_for_task(db_session, task.task_id, datetime.now(UTC))

                # Re-queue the task for retry
                ok = await update_task_status(db_session, task.task_id, "queued")
                if ok:
                    recovered.append(task.task_id)
                    logger.info(
                        "Recovered stale task",
                        extra={"extra_data": {"task_id": task.task_id}},
                    )

            await db_session.commit()

        return recovered

    async def recover_paused_tasks(self) -> list[str]:
        """Re-enter paused workflows so prompts and waits are recreated after restart."""
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
            if pause_type == "gate":
                self._launch_task_run(task)
            elif pause_type == "step_input":
                payload = task.workflow_state.pending_pause_payload or {}
                self._workflow_engine._pause_waiter.register(  # noqa: SLF001
                    PendingPause(
                        pause_id=str(payload.get("pause_id", f"recovered_{task.task_id}")),
                        pause_type="step_input",
                        task_id=task.task_id,
                        step_name=payload.get("step_name"),
                        step_run_id=payload.get("step_run_id"),
                        session_id=payload.get("session_id"),
                        question=payload.get("question"),
                        options=(
                            [
                                {"label": str(item), "action": str(item)}
                                for item in payload.get("options", [])
                            ]
                            if isinstance(payload.get("options"), list)
                            else None
                        ),
                        context=(
                            {"context": payload.get("context")}
                            if isinstance(payload.get("context"), str)
                            else None
                        ),
                    )
                )
            recovered.append(task.task_id)

        return recovered

    async def _drain_loop(self) -> None:
        """Main queue processing loop."""
        while not self._stop_event.is_set():
            try:
                # Wait for wake signal or poll interval
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=self._poll_interval,
                    )
                self._wake_event.clear()

                if self._stop_event.is_set():
                    break

                # Clean up completed runs
                for task_id in list(self._active_runs):
                    if self._active_runs[task_id].done():
                        self._active_runs.pop(task_id)
                        self._run_controls.pop(task_id, None)

                # Check capacity
                if not await self._has_capacity():
                    continue

                # Try to pick and run a task
                await self._try_pick_and_run()

            except Exception:
                logger.exception("Task queue drain loop error")
                await asyncio.sleep(self._poll_interval)

    async def _try_pick_and_run(self) -> None:
        """Try to pick the next ready task and start executing it."""
        async with self._pick_lock, self._session_factory() as db_session:
            task_row = await pick_ready_task(db_session)
            if task_row is None:
                return
            await db_session.commit()

            task = _row_to_task_model(task_row)

        # Check per-agent capacity after picking
        if not await self._has_capacity(agent_id=task.agent_id):
            # Agent is saturated — put the task back to ready
            async with self._session_factory() as db_session:
                await update_task_status(db_session, task.task_id, "ready")
                await db_session.commit()
            return

        TASKS_TOTAL.labels(status="running").inc()

        await self._event_bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                data={"task_id": task.task_id},
            )
        )

        # Start workflow execution in background
        self._launch_task_run(task)

    async def _select_workflow_for_task(self, task: TaskModel) -> str:
        """Auto-select a workflow using the LLM classifier.

        Falls back to ``system:direct`` if the classifier is unavailable
        or no workflows match.  Persists the selection to the DB so the
        user can see which workflow was chosen.
        """
        if self._llm_provider is None:
            return "system:direct"

        from cognis.core.decision import select_workflow

        try:
            available = await self._workflow_registry.list_all()
            if not available:
                return "system:direct"

            selection = await select_workflow(
                llm=self._llm_provider,
                task_description=task.description or task.title,
                available_workflows=[
                    {
                        "workflow_id": w.workflow_id,
                        "name": w.name,
                        "criteria": w.criteria,
                    }
                    for w in available
                ],
                default_workflow_id="system:research",
            )
            workflow_id = selection.workflow_id
            logger.info(
                "Auto-selected workflow for task",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "workflow_id": workflow_id,
                        "confidence": selection.confidence,
                        "reason": selection.reason,
                    }
                },
            )
        except Exception:
            logger.warning(
                "Workflow auto-selection failed, falling back to system:direct",
                extra={"extra_data": {"task_id": task.task_id}},
                exc_info=True,
            )
            workflow_id = "system:direct"

        # Persist the selected workflow on the task for visibility
        task.workflow_id = workflow_id
        async with self._session_factory() as db_session:
            row = await get_task(db_session, task.task_id)
            if row is not None:
                row.workflow_id = workflow_id
                await db_session.commit()

        return workflow_id

    async def _run_task(self, task: TaskModel) -> None:
        """Execute a task's workflow.

        Runs inside ``scoped_runtime_context`` so that all downstream
        Intaris calls (event recording, evaluation, delivery) use the
        correct user identity instead of the default ``system@example.com``.
        """
        cancel_event = self._run_controls.setdefault(task.task_id, asyncio.Event())
        with scoped_runtime_context(
            user_email=task.created_by,
            agent_id=task.agent_id,
        ):
            try:
                # Resolve workflow — auto-select via LLM classifier if not set
                if task.workflow_id:
                    workflow_id = task.workflow_id
                else:
                    workflow_id = await self._select_workflow_for_task(task)
                workflow = await self._workflow_registry.get(workflow_id)
                if workflow is None:
                    logger.warning(
                        "Unknown workflow for task",
                        extra={"extra_data": {"task_id": task.task_id, "workflow_id": workflow_id}},
                    )
                    async with self._session_factory() as db_session:
                        await update_task_status(
                            db_session,
                            task.task_id,
                            "failed",
                            result_summary=f"Unknown workflow: {workflow_id}",
                            completed_at=datetime.now(UTC),
                        )
                        await db_session.commit()
                    return

                # Initialize workflow state
                task.workflow_state = task.workflow_state or WorkflowState()
                async with self._session_factory() as db_session:
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

                # Resolve dependencies
                if result.status == TaskStatus.COMPLETED:
                    await self.resolve_dependencies(result.task_id)

            except asyncio.CancelledError:
                logger.info(
                    "Task execution cancelled",
                    extra={"extra_data": {"task_id": task.task_id}},
                )
                async with self._session_factory() as db_session:
                    task_row = await get_task(db_session, task.task_id)
                    current_status = str(task_row.status) if task_row is not None else "failed"
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
            except Exception:
                logger.exception(
                    "Task execution failed",
                    extra={"extra_data": {"task_id": task.task_id}},
                )
                async with self._session_factory() as db_session:
                    await update_task_status(
                        db_session,
                        task.task_id,
                        "failed",
                        completed_at=datetime.now(UTC),
                    )
                    await db_session.commit()
            finally:
                self._active_runs.pop(task.task_id, None)
                self._run_controls.pop(task.task_id, None)
                # Update queue depth metric
                async with self._session_factory() as db_session:
                    queued = await list_tasks_by_status(db_session, ["queued", "ready"])
                QUEUE_DEPTH.labels(queue="default").set(len(queued))

    async def _has_capacity(self, agent_id: str | None = None) -> bool:
        """Check if there's capacity to run another step.

        Enforces both global and per-agent limits.
        """
        async with self._session_factory() as db_session:
            active_global = await count_active_steps(db_session)
            if active_global >= self._max_active_global:
                return False
            if agent_id is not None:
                active_for_agent = await count_active_steps(db_session, agent_id=agent_id)
                if active_for_agent >= self._max_active_per_agent:
                    return False
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
                    return True
            await db_session.commit()
        return False

    def _launch_task_run(self, task: TaskModel) -> None:
        """Start or restart a task execution coroutine."""
        if task.task_id in self._active_runs and not self._active_runs[task.task_id].done():
            return
        self._run_controls[task.task_id] = asyncio.Event()
        run_task = asyncio.create_task(self._run_task(task))
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
        status=TaskStatus(row.status),
        priority=row.priority,
        created_by=row.created_by,
        agent_id=row.agent_id,
        source_type=row.source_type,
        source_ref=row.source_ref,
        delivery=TaskDelivery(
            mode=row.delivery_mode,
            target=row.delivery_target,
        ),
        workflow_id=row.workflow_id,
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
    )
