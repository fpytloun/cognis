"""Workflow engine — orchestrates direct turns and workflow steps.

Manages the between-step layer: step sequencing, gates, review loops,
evaluation, and pause/resume. Uses the AgentLoop for within-step
execution.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import (
    AgentLoop,
    PauseWaiter,
    PendingPause,
    StepContext,
    StepInterrupted,
    TokenCallback,
    ToolCallCallback,
    ToolResultCallback,
)
from cognis.core.events import Event, EventBus, EventType
from cognis.core.step_evaluator import StepEvaluator
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import SessionEvent
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import (
    CompletionConfig,
    StepDefinition,
    StepEvaluation,
    StepOutput,
    Workflow,
    WorkflowState,
    resolve_source_names,
)
from cognis.store.queries import (
    create_step_run,
    get_agent,
    get_latest_active_conversation_for_agent,
    get_latest_step_run_for_task_step,
    update_step_run,
    update_task_status,
    update_task_workflow_state,
)

logger = get_logger(__name__)

# Prometheus metrics
WORKFLOWS_TOTAL = Counter(
    "cognis_workflows_total",
    "Workflow executions",
    labelnames=("workflow_name", "status"),
)
WORKFLOW_DURATION = Histogram(
    "cognis_workflow_duration_seconds",
    "Workflow duration",
    labelnames=("workflow_name",),
)
GATES_TOTAL = Counter(
    "cognis_workflow_gates_total",
    "Gate events",
    labelnames=("action",),
)
REVIEW_LOOPS = Counter(
    "cognis_workflow_review_loops_total",
    "Review loop iterations",
    labelnames=("step_name",),
)

# Callback type for progress notifications
ProgressCallback = TokenCallback


async def _noop_cleanup() -> None:
    """No-op cleanup callback for shared runtimes."""


class WorkflowEngine:
    """Orchestrates step execution for a task."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        providers: Any,
        agent_loop: AgentLoop,
        step_evaluator: StepEvaluator,
        workflow_registry: WorkflowRegistry,
        session_manager: Any,
        event_bus: EventBus,
        pause_waiter: PauseWaiter,
        step_runtime_factory: Any = None,
        shared_tool_registry: Any = None,
        shared_executor_connection: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._providers = providers
        self._agent_loop = agent_loop
        self._step_evaluator = step_evaluator
        self._workflow_registry = workflow_registry
        self._session_manager = session_manager
        self._event_bus = event_bus
        self._pause_waiter = pause_waiter
        self._step_runtime_factory = step_runtime_factory
        self._shared_tool_registry = shared_tool_registry
        self._shared_executor_connection = shared_executor_connection

    async def run_direct_turn(
        self,
        *,
        conversation: Any,
        session: Any,
        agent: AgentDefinition,
        user_message: str,
        system_initiated: bool = False,
        on_progress: ProgressCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> StepOutput | None:
        """Run the hot-path direct workflow through a workflow-engine entrypoint.

        Direct turns stay single-step and do not create Task or StepRun rows,
        but the engine remains the owner of the orchestration entrypoint so
        metrics, runtime resolution, and future hooks stay centralized.
        """
        tool_registry, executor_connection, cleanup = await self._resolve_step_runtime(
            agent=agent,
            user_email=session.user_email,
        )
        from cognis.tools.builtin.orchestration import OrchestrationMode

        direct_step = StepDefinition(name="direct", type="run", prompt=user_message)
        ctx = StepContext(
            step_definition=direct_step,
            session=session,
            conversation=conversation,
            agent=agent,
            is_direct=True,
            user_message=user_message,
            system_initiated=system_initiated,
            interaction_mode="explicit_gates",
            tool_registry=tool_registry,
            executor_connection=executor_connection,
            cancel_event=cancel_event,
            orchestration_mode=OrchestrationMode.FULL,
        )

        try:
            return await self._agent_loop.run_step(
                ctx,
                on_token=on_progress,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
        finally:
            await cleanup()

    async def execute_workflow(
        self,
        task: TaskModel,
        workflow: Workflow,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> TaskModel:
        """Execute a full workflow for a task.

        Iterates through workflow steps in sequence, running each via the
        agent loop, evaluating results, handling gates and review loops.

        Returns the updated TaskModel.
        """
        start_time = datetime.now(UTC)
        state = task.workflow_state or WorkflowState()
        task.workflow_state = state

        logger.info(
            "Starting workflow execution",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "workflow": workflow.name,
                    "step_count": len(workflow.steps),
                }
            },
        )

        try:
            while state.current_step_index < len(workflow.steps):
                step_def = workflow.steps[state.current_step_index]
                state.current_step_status = "running"

                if step_def.type == "gate":
                    gate_result = await self._handle_gate_step(task, step_def, state, workflow)
                    if gate_result == "cancel":
                        state.status = "failed"
                        break
                    revise_target = _parse_revise_action(gate_result)
                    if revise_target is not None:
                        target_idx = self._find_step_index(workflow, revise_target)
                        if target_idx is not None:
                            state.current_step_index = target_idx
                            continue
                    # "continue" → advance
                    state.current_step_index += 1
                    await self._persist_workflow_state(task)
                    continue

                # Run step
                step_result = await self._execute_run_step(
                    task,
                    step_def,
                    state,
                    workflow,
                    on_progress=on_progress,
                    cancel_event=cancel_event,
                )

                if step_result is None:
                    # Step failed without producing output
                    exhausted_action = self._get_on_exhausted(step_def, workflow)
                    handled = await self._handle_exhausted(
                        task, step_def, state, workflow, exhausted_action
                    )
                    if not handled:
                        state.status = "failed"
                        break
                    continue

                # Store step output
                state.step_outputs[step_def.name] = step_result.model_dump(mode="json")

                # Evaluate if configured
                completion = self._resolve_completion(step_def, workflow)
                if completion and completion.evaluate:
                    evaluation = await self._evaluate_step(
                        step_def, step_result, state, task, workflow
                    )

                    if evaluation.decision == "revise":
                        # Store feedback for retry
                        state.last_evaluation_feedback = evaluation.feedback or evaluation.reasoning

                        # Check on_reject config for review loops
                        if step_def.on_reject:
                            loop_handled = await self._handle_review_loop(
                                task, step_def, state, workflow
                            )
                            if loop_handled:
                                continue

                        # No review loop or loop exhausted — re-attempt this step
                        handled = await self._handle_step_revision(
                            task, step_def, state, workflow, evaluation
                        )
                        if not handled:
                            state.status = "failed"
                            break
                        continue

                    elif evaluation.decision == "failed":
                        exhausted_action = self._get_on_exhausted(step_def, workflow)
                        handled = await self._handle_exhausted(
                            task, step_def, state, workflow, exhausted_action
                        )
                        if not handled:
                            state.status = "failed"
                            break
                        continue

                # Step approved or no evaluation — advance
                state.current_step_status = None
                state.pending_pause_type = None
                state.pending_pause_payload = None
                state.current_step_index += 1
                await self._persist_workflow_state(task)

                await self._event_bus.publish(
                    Event(
                        type=EventType.STEP_COMPLETED,
                        data={
                            "task_id": task.task_id,
                            "step_name": step_def.name,
                            "step_index": state.current_step_index - 1,
                        },
                    )
                )

            # Workflow completed
            if state.status != "failed":
                state.status = "completed"
                state.current_step_status = None
                state.pending_pause_type = None
                state.pending_pause_payload = None
                task.status = TaskStatus.COMPLETED
                task.result_summary = self._build_result_summary(state, workflow)
                task.completed_at = datetime.now(UTC)
            else:
                state.current_step_status = None
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now(UTC)

            await self._persist_task_final(task)

            WORKFLOWS_TOTAL.labels(
                workflow_name=workflow.name,
                status=task.status,
            ).inc()

        except StepInterrupted:
            current_status = await self._read_task_status(task.task_id)
            if current_status == TaskStatus.CANCELLED:
                state.status = "cancelled"
                state.current_step_status = None
                state.pending_pause_type = None
                state.pending_pause_payload = None
                task.status = TaskStatus.CANCELLED
                task.completed_at = datetime.now(UTC)
                await self._persist_task_final(task)
                WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status=task.status).inc()
            else:
                state.status = "paused"
                task.status = TaskStatus.PAUSED
                await self._persist_workflow_state(task)
                WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status=task.status).inc()
        except Exception:
            logger.exception(
                "Workflow execution failed",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            state.status = "failed"
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now(UTC)
            await self._persist_task_final(task)
            WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status="failed").inc()

        duration = (datetime.now(UTC) - start_time).total_seconds()
        WORKFLOW_DURATION.labels(workflow_name=workflow.name).observe(duration)

        # Deliver result
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            await self._deliver_task_result(task)

        return task

    async def _execute_run_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> StepOutput | None:
        """Execute a single run step via the agent loop."""

        # Resolve agent
        agent = await self._resolve_step_agent(task, step_def)
        if agent is None:
            logger.warning(
                "Could not resolve agent for step",
                extra={"extra_data": {"task_id": task.task_id, "step": step_def.name}},
            )
            return None

        # Determine if this is a retry (re-attempt of a previously-run step)
        attempt = state.loop_iterations.get(f"attempts:{step_def.name}", 1)
        is_retry = attempt > 1

        # Determine step index
        step_index = self._find_step_index(workflow, step_def.name) or 0

        # Session handling: reuse on retry, create new on first attempt
        if is_retry:
            conversation, session = await self._reuse_or_create_step_session(task, step_def, agent)
        else:
            conversation, session = await self._create_step_session(task, step_def, agent)

        # Create StepRun record
        step_run_id = f"sr_{uuid.uuid4().hex}"
        async with self._session_factory() as db_session:
            await create_step_run(
                db_session,
                task_id=task.task_id,
                step_name=step_def.name,
                step_type=step_def.type,
                agent_id=agent.agent_id,
                attempt=attempt,
                step_run_id=step_run_id,
            )
            await update_step_run(
                db_session,
                step_run_id,
                status="running",
                session_id=session.session_id,
                intaris_session_id=session.intaris_session_id,
                started_at=datetime.now(UTC),
            )
            await db_session.commit()

        await self._event_bus.publish(
            Event(
                type=EventType.STEP_STARTED,
                data={
                    "task_id": task.task_id,
                    "step_name": step_def.name,
                    "step_run_id": step_run_id,
                },
            )
        )

        # Resolve tool registry and executor for this step
        tool_registry, executor_connection, cleanup = await self._resolve_step_runtime(
            agent=agent,
            user_email=task.created_by,
        )

        from cognis.tools.builtin.orchestration import OrchestrationMode

        # Build step context — task steps can delegate (sync only)
        ctx = StepContext(
            step_definition=step_def,
            session=session,
            conversation=conversation,
            agent=agent,
            task_id=task.task_id,
            task_title=task.title,
            task_description=task.description,
            step_run_id=step_run_id,
            is_direct=False,
            is_retry=is_retry,
            user_message=step_def.prompt,
            interaction_mode=workflow.interaction.mode,
            tool_registry=tool_registry,
            executor_connection=executor_connection,
            workflow_state=state,
            workflow_steps=workflow.steps,
            step_index=step_index,
            cancel_event=cancel_event,
            orchestration_mode=OrchestrationMode.DELEGATE_SYNC_ONLY,
        )

        # Run agent loop
        try:
            output = await self._agent_loop.run_step(ctx, on_token=on_progress)
        except StepInterrupted:
            current_status = await self._read_task_status(task.task_id)
            async with self._session_factory() as db_session:
                await update_step_run(
                    db_session,
                    step_run_id,
                    status="cancelled" if current_status == TaskStatus.CANCELLED else "paused",
                    completed_at=datetime.now(UTC),
                )
                await db_session.commit()
            raise
        finally:
            await cleanup()

        # Enrich output with session metadata
        if output is not None:
            output.completed_at = datetime.now(UTC)
            output.session_id = session.session_id
            output.intaris_session_id = session.intaris_session_id

        # Update StepRun record
        async with self._session_factory() as db_session:
            await update_step_run(
                db_session,
                step_run_id,
                status="approved" if output else "failed",
                output=output.model_dump(mode="json") if output else None,
                completed_at=datetime.now(UTC),
            )
            await db_session.commit()

        return output

    async def _evaluate_step(
        self,
        step_def: StepDefinition,
        step_output: StepOutput,
        state: WorkflowState,
        task: TaskModel,
        workflow: Workflow,
    ) -> StepEvaluation:
        """Run the step evaluator."""
        step_index = self._find_step_index(workflow, step_def.name) or 0
        source_names = resolve_source_names(step_def, step_index, workflow.steps)

        step_inputs: dict[str, StepOutput] = {}
        for source_name in source_names:
            raw = state.step_outputs.get(source_name)
            if raw:
                step_inputs[source_name] = StepOutput.model_validate(raw)

        return await self._step_evaluator.evaluate(
            step_definition=step_def,
            step_output=step_output,
            step_inputs=step_inputs,
            task_context=task.description,
        )

    async def _handle_gate_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
    ) -> str:
        """Handle a gate step — pause and wait for caller response."""
        if workflow.interaction.mode == "none":
            # Autonomous mode — gates become continue
            GATES_TOTAL.labels(action="auto_continue").inc()
            return "continue"

        gate = step_def.gate
        if gate is None:
            return "continue"

        # Build gate context from specified inputs
        gate_context: dict[str, Any] = {}
        for input_name in gate.input:
            raw = state.step_outputs.get(input_name)
            if raw:
                gate_context[input_name] = raw

        # Pause workflow
        state.status = "paused"
        state.current_step_status = "paused"
        pause_id = f"gate_{uuid.uuid4().hex[:12]}"
        state.pending_pause_type = "gate"
        state.pending_pause_payload = {
            "pause_id": pause_id,
            "task_id": task.task_id,
            "step_name": step_def.name,
            "message": gate.message,
            "context": gate_context,
            "options": [opt.model_dump(mode="json") for opt in gate.options],
        }
        await self._persist_workflow_state(task)

        async with self._session_factory() as db_session:
            await update_task_status(db_session, task.task_id, "paused")
            await db_session.commit()

        self._pause_waiter.register(
            PendingPause(
                pause_id=pause_id,
                pause_type="gate",
                task_id=task.task_id,
                step_name=step_def.name,
                question=gate.message,
                options=[opt.model_dump(mode="json") for opt in gate.options],
                context=gate_context,
            )
        )
        await self._event_bus.publish(
            Event(
                type=EventType.WORKFLOW_GATE,
                data={
                    "pause_id": pause_id,
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "message": gate.message,
                    "context": gate_context,
                    "options": [opt.model_dump(mode="json") for opt in gate.options],
                },
            )
        )

        # Wait for resolution
        try:
            resolution = await self._pause_waiter.wait(pause_id, timeout=3600.0)
            action = resolution.decision
        except TimeoutError:
            action = "continue"  # Default on timeout

        GATES_TOTAL.labels(action=action).inc()

        # Resume
        state.status = "running"
        state.current_step_status = "running"
        state.pending_pause_type = None
        state.pending_pause_payload = None
        await self._persist_workflow_state(task)
        async with self._session_factory() as db_session:
            await update_task_status(db_session, task.task_id, "running")
            await db_session.commit()

        if action.startswith("revise"):
            return action
        return action

    async def _handle_review_loop(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
    ) -> bool:
        """Handle a review loop (on_reject → target step).

        Returns True if the loop was handled (workflow should continue from
        the target step), False if the loop is exhausted.
        """
        on_reject = step_def.on_reject
        if on_reject is None:
            return False

        loop_key = f"{on_reject.target}->{step_def.name}"
        current_iterations = state.loop_iterations.get(loop_key, 0)

        if current_iterations >= on_reject.max_loop_iterations:
            # Loop exhausted
            exhausted_action = on_reject.on_exhausted
            return await self._handle_exhausted(task, step_def, state, workflow, exhausted_action)

        REVIEW_LOOPS.labels(step_name=step_def.name).inc()
        state.loop_iterations[loop_key] = current_iterations + 1

        # Jump back to the target step
        target_idx = self._find_step_index(workflow, on_reject.target)
        if target_idx is not None:
            state.current_step_index = target_idx
            await self._persist_workflow_state(task)
            return True

        return False

    async def _handle_step_revision(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        evaluation: StepEvaluation,
    ) -> bool:
        """Handle step revision (re-attempt with feedback).

        Records evaluation feedback to the existing step Intaris session
        (so the agent sees it on retry), then increments the attempt
        counter.

        Returns True if revision is possible, False if exhausted.
        """
        completion = self._resolve_completion(step_def, workflow)
        max_attempts = completion.max_attempts if completion else 3

        # Count attempts for this step
        attempt_key = f"attempts:{step_def.name}"
        current_attempts = state.loop_iterations.get(attempt_key, 1)

        if current_attempts >= max_attempts:
            exhausted_action = self._get_on_exhausted(step_def, workflow)
            return await self._handle_exhausted(task, step_def, state, workflow, exhausted_action)

        # Record evaluation feedback to the existing step session in Intaris
        await self._record_evaluation_feedback(task, step_def, state, evaluation)

        state.loop_iterations[attempt_key] = current_attempts + 1
        await self._persist_workflow_state(task)
        # Stay on the same step — the main loop will re-execute it
        return True

    async def _record_evaluation_feedback(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        evaluation: StepEvaluation,
    ) -> None:
        """Append evaluation feedback event to the step's Intaris session.

        If Intaris recording fails, fall back to
        ``state.last_evaluation_feedback`` which will be consumed by the
        agent loop on retry (fail-open for feedback).
        """
        attempt = state.loop_iterations.get(f"attempts:{step_def.name}", 1)
        feedback_text = evaluation.feedback or evaluation.reasoning

        # Look up the step's Intaris session
        try:
            async with self._session_factory() as db_session:
                prior_run = await get_latest_step_run_for_task_step(
                    db_session, task.task_id, step_def.name
                )
            if prior_run is None or prior_run.intaris_session_id is None:
                # No prior run — store feedback in state as fallback
                state.last_evaluation_feedback = feedback_text
                return

            event = SessionEvent(
                type="evaluation",
                data={
                    "event": "evaluation_feedback",
                    "attempt": attempt,
                    "decision": evaluation.decision,
                    "feedback": feedback_text,
                },
            )
            await self._providers.guardrails.record_events(
                session_id=prior_run.intaris_session_id,
                events=[event],
                source="cognis",
            )
            # Clear in-state fallback — the event is now in Intaris
            state.last_evaluation_feedback = None
        except Exception:
            logger.warning(
                "Failed to record evaluation feedback to Intaris, using state fallback",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step_name": step_def.name,
                    }
                },
            )
            state.last_evaluation_feedback = feedback_text

    async def _handle_exhausted(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        action: str,
    ) -> bool:
        """Handle exhausted attempts/loops.

        Returns True if handled (workflow continues), False if workflow should fail.
        """
        if action == "continue":
            # Skip and advance
            state.current_step_index += 1
            await self._persist_workflow_state(task)
            return True

        elif action == "gate":
            if workflow.interaction.mode == "none":
                # Autonomous mode — gate becomes fail
                return False
            # Create a gate for user decision
            result = await self._handle_gate_step(
                task,
                StepDefinition(
                    name=f"{step_def.name}_exhausted",
                    type="gate",
                    gate=_build_exhaustion_gate(step_def),
                ),
                state,
                workflow,
            )
            if result == "continue":
                state.current_step_index += 1
                await self._persist_workflow_state(task)
                return True
            elif result == "cancel":
                return False
            revise_target = _parse_revise_action(result)
            if revise_target is not None:
                target_idx = self._find_step_index(workflow, revise_target)
                if target_idx is not None:
                    state.current_step_index = target_idx
                    await self._persist_workflow_state(task)
                    return True
            return False

        else:  # "fail"
            return False

    async def _deliver_task_result(self, task: TaskModel) -> None:
        """Resolve delivery target and inject synthetic event."""
        delivery_mode = task.delivery.mode
        target_conversation_id: str | None = None

        if delivery_mode == "same_conversation":
            target_conversation_id = task.source_ref
        elif delivery_mode == "specific_conversation":
            target_conversation_id = task.delivery.target
        elif delivery_mode in ("latest_active_for_agent", "preferred_channel"):
            async with self._session_factory() as db_session:
                latest = await get_latest_active_conversation_for_agent(
                    db_session, task.created_by, task.agent_id
                )
            target_conversation_id = (
                latest.conversation_id if latest is not None else task.source_ref
            )
        elif delivery_mode == "silent":
            return

        if target_conversation_id is None:
            return

        task_event = {
            TaskStatus.COMPLETED: "task_result",
            TaskStatus.FAILED: "task_failed",
            TaskStatus.CANCELLED: "task_cancelled",
        }.get(task.status, "task_status")

        event = SessionEvent(
            type="lifecycle",
            data={
                "event": task_event,
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status,
                "result_summary": task.result_summary,
            },
        )

        # Record to target conversation's Intaris session
        recorded_to_intaris = False
        try:
            # Resolve the conversation's root session Intaris ID
            async with self._session_factory() as db_session:
                from cognis.store.queries import get_conversation

                conv = await get_conversation(db_session, target_conversation_id)
                if conv and conv.root_session_id:
                    from cognis.store.queries import get_session_row

                    sess = await get_session_row(db_session, conv.root_session_id)
                    if sess and sess.intaris_session_id:
                        await self._providers.guardrails.record_events(
                            session_id=sess.intaris_session_id,
                            events=[event],
                            source="cognis",
                        )
                        recorded_to_intaris = True
        except Exception:
            logger.warning(
                "Failed to deliver task result to conversation",
                extra={"extra_data": {"task_id": task.task_id}},
            )

        # Publish event for WebSocket delivery
        event_type = EventType.TASK_FAILED
        if task.status == TaskStatus.COMPLETED:
            event_type = EventType.TASK_COMPLETED
        elif task.status == TaskStatus.CANCELLED:
            event_type = EventType.TASK_CANCELLED
        await self._event_bus.publish(
            Event(
                type=event_type,
                data={
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "title": task.title,
                    "conversation_id": target_conversation_id,
                    "result_summary": task.result_summary,
                },
            )
        )
        if recorded_to_intaris:
            await self._event_bus.publish(
                Event(
                    type=EventType.FOLLOW_UP_TURN_REQUESTED,
                    data={
                        "conversation_id": target_conversation_id,
                        "task_id": task.task_id,
                        "agent_id": task.agent_id,
                        "user_email": task.created_by,
                        "status": str(task.status),
                    },
                )
            )

    async def _persist_workflow_state(self, task: TaskModel) -> None:
        """Persist workflow state to DB after a step transition."""
        if task.workflow_state is None:
            return
        async with self._session_factory() as db_session:
            await update_task_workflow_state(
                db_session,
                task.task_id,
                task.workflow_state.model_dump(mode="json"),
            )
            await db_session.commit()

    async def _persist_task_final(self, task: TaskModel) -> None:
        """Persist final task state (completed/failed)."""
        async with self._session_factory() as db_session:
            await update_task_status(
                db_session,
                task.task_id,
                task.status,
                completed_at=task.completed_at,
                result_summary=task.result_summary,
                result_data=task.result_data,
            )
            if task.workflow_state:
                await update_task_workflow_state(
                    db_session,
                    task.task_id,
                    task.workflow_state.model_dump(mode="json"),
                )
            await db_session.commit()

    async def _read_task_status(self, task_id: str) -> TaskStatus:
        """Read the latest persisted task status."""
        from cognis.store.queries import get_task

        async with self._session_factory() as db_session:
            row = await get_task(db_session, task_id)
        if row is None:
            return TaskStatus.FAILED
        return TaskStatus(str(row.status))

    async def _resolve_step_runtime(
        self,
        *,
        agent: AgentDefinition,
        user_email: str,
    ) -> tuple[Any, Any, Any]:
        """Resolve the tool registry and executor connection for one step/turn."""
        if callable(self._step_runtime_factory):
            return cast(
                tuple[Any, Any, Any],
                await self._step_runtime_factory(agent=agent, user_email=user_email),
            )

        return (
            self._shared_tool_registry,
            self._shared_executor_connection,
            _noop_cleanup,
        )

    async def _resolve_step_agent(
        self,
        task: TaskModel,
        step_def: StepDefinition,
    ) -> AgentDefinition | None:
        """Resolve which agent runs a step."""
        # Check step_agent_overrides from agent's execution config
        async with self._session_factory() as db_session:
            agent_row = await get_agent(db_session, task.agent_id)
        if agent_row is None:
            return None

        from cognis.models.agent import AgentDefinition

        return AgentDefinition.model_validate(
            {c.name: getattr(agent_row, c.name) for c in agent_row.__table__.columns}
        )

    async def _create_step_session(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        agent: AgentDefinition,
    ) -> tuple[Any, Any]:
        """Create a conversation and session for a workflow step."""
        from cognis.models.session import ConversationContext

        context = ConversationContext(
            type="task",
            ref=task.task_id,
        )
        conversation, session = await self._session_manager.create_conversation_with_root_session(
            user_email=task.created_by,
            agent_id=agent.agent_id,
            context=context,
            title=f"Task: {task.title} / Step: {step_def.name}",
            intention=f"Task: {task.title} — Step: {step_def.name} — {step_def.description or step_def.prompt[:100]}",
        )
        return conversation, session

    async def _reuse_or_create_step_session(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        agent: AgentDefinition,
    ) -> tuple[Any, Any]:
        """Reuse the prior step session on retry, or create a new one.

        On retry, the spec requires continuing the same Intaris session
        so the agent keeps its prior work and sees evaluation feedback.
        """
        try:
            async with self._session_factory() as db_session:
                prior_run = await get_latest_step_run_for_task_step(
                    db_session, task.task_id, step_def.name
                )
            if prior_run is not None and prior_run.session_id is not None:
                # Recover session and conversation from the prior run
                from cognis.store.queries import get_conversation, get_session_row

                async with self._session_factory() as db_session:
                    session_row = await get_session_row(db_session, prior_run.session_id)
                    if session_row is not None:
                        conv_row = await get_conversation(db_session, session_row.conversation_id)
                        if conv_row is not None:
                            from cognis.models.session import ConversationModel, SessionModel

                            conversation = ConversationModel.model_validate(
                                {
                                    c.name: getattr(conv_row, c.name)
                                    for c in conv_row.__table__.columns
                                }
                            )
                            session = SessionModel.model_validate(
                                {
                                    c.name: getattr(session_row, c.name)
                                    for c in session_row.__table__.columns
                                }
                            )
                            return conversation, session
        except Exception:
            logger.warning(
                "Could not reuse prior step session, creating new one",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step_name": step_def.name,
                    }
                },
            )

        # Fallback — create a fresh session
        return await self._create_step_session(task, step_def, agent)

    def _resolve_completion(
        self, step_def: StepDefinition, workflow: Workflow
    ) -> CompletionConfig | None:
        """Resolve completion config with workflow defaults."""
        if step_def.completion is not None:
            return step_def.completion
        if workflow.defaults.evaluate:
            return CompletionConfig(
                evaluate=workflow.defaults.evaluate,
                max_attempts=workflow.defaults.max_attempts,
                on_exhausted=workflow.defaults.on_exhausted,
            )
        return None

    def _get_on_exhausted(self, step_def: StepDefinition, workflow: Workflow) -> str:
        """Get the on_exhausted action for a step."""
        if step_def.completion and step_def.completion.on_exhausted:
            return step_def.completion.on_exhausted
        return workflow.defaults.on_exhausted

    def _find_step_index(self, workflow: Workflow, step_name: str) -> int | None:
        """Find a step's index by name."""
        for i, step in enumerate(workflow.steps):
            if step.name == step_name:
                return i
        return None

    def _build_result_summary(self, state: WorkflowState, workflow: Workflow) -> str:
        """Build a result summary from the last step's output."""
        if not workflow.steps:
            return ""
        last_step = workflow.steps[-1]
        raw_output = state.step_outputs.get(last_step.name)
        if raw_output and isinstance(raw_output, dict):
            return str(raw_output.get("summary", ""))
        return ""


def _build_exhaustion_gate(step_def: StepDefinition) -> Any:
    """Build a gate config for exhausted step attempts."""
    from cognis.models.workflow import GateConfig, GateOption

    return GateConfig(
        message=f"Step '{step_def.name}' has exhausted its retry limit. How would you like to proceed?",
        options=[
            GateOption(label="Continue anyway", action="continue"),
            GateOption(label="Cancel workflow", action="cancel"),
        ],
    )


def _parse_revise_action(action: str) -> str | None:
    """Parse a revise action and return the target step name.

    Handles both 'revise(step_name)' and 'revise:step_name' formats.
    Returns None if the action is not a revise action.
    """
    if action.startswith("revise(") and action.endswith(")"):
        return action[7:-1]
    if action.startswith("revise:"):
        return action[7:]
    return None
