"""Workflow engine — orchestrates direct turns and workflow steps.

Manages the between-step layer: step sequencing, gates, review loops,
evaluation, and pause/resume. Uses the AgentLoop for within-step
execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import (
    CHAT_POLICY,
    SECONDARY_POLICY,
    WORKFLOW_POLICY,
    AgentLoop,
    PauseWaiter,
    StepContext,
    StepInterrupted,
    TokenCallback,
    ToolCallCallback,
    ToolResultCallback,
)
from cognis.core.events import Event, EventBus, EventType
from cognis.core.runtime import ResolvedStepRuntime
from cognis.core.step_evaluator import StepEvaluator
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import AttachmentRef
from cognis.models.session import SessionEvent
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.workflow import (
    CompletionConfig,
    StepDefinition,
    StepEvaluation,
    StepOutput,
    Workflow,
    WorkflowState,
    resolve_effective_input,
    resolve_source_names,
)
from cognis.store.queries import (
    create_step_run,
    get_latest_active_conversation_for_agent,
    get_latest_step_run_for_task_step,
    update_step_run,
    update_task_status,
    update_task_workflow_state,
)
from cognis.tools.builtin.orchestration import OrchestrationMode

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
        session_cache: Any = None,
        notification_service: Any = None,
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
        self._session_cache = session_cache
        self._shared_executor_connection = shared_executor_connection
        self._notification_service = notification_service

    async def run_direct_turn(
        self,
        *,
        conversation: Any,
        session: Any,
        agent: AgentDefinition,
        user_message: str,
        user_attachments: list[AttachmentRef] | None = None,
        attachment_notice: str | None = None,
        system_initiated: bool = False,
        on_progress: ProgressCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        bootstrap_wait_for_intention: bool = False,
    ) -> StepOutput | None:
        """Run the hot-path direct workflow through a workflow-engine entrypoint.

        Direct turns stay single-step and do not create Task or StepRun rows,
        but the engine remains the owner of the orchestration entrypoint so
        metrics, runtime resolution, and future hooks stay centralized.
        """
        runtime = await self._resolve_step_runtime(
            agent=agent,
            user_email=session.user_email,
        )

        direct_step = StepDefinition(
            name="direct",
            type="run",
            prompt=user_message,
            allow_questions=True,
        )
        ctx = StepContext(
            step_definition=direct_step,
            session=session,
            conversation=conversation,
            agent=agent,
            policy=CHAT_POLICY,
            user_message=user_message,
            user_attachments=user_attachments or [],
            attachment_notice=attachment_notice,
            system_initiated=system_initiated,
            interaction_mode="step_requests",
            tool_registry=runtime.tool_registry,
            executor_connection=runtime.executor_connection,
            executor_environment=runtime.executor_environment,
            cancel_event=cancel_event,
            bootstrap_wait_for_intention=bootstrap_wait_for_intention,
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
            await runtime.cleanup()

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
        max_workflow_seconds = 14400.0  # 4 hours default
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
                # Check overall workflow timeout
                elapsed = (datetime.now(UTC) - start_time).total_seconds()
                if elapsed > max_workflow_seconds:
                    logger.error(
                        "Workflow execution timed out",
                        extra={
                            "extra_data": {
                                "task_id": task.task_id,
                                "elapsed_seconds": elapsed,
                                "max_seconds": max_workflow_seconds,
                            }
                        },
                    )
                    state.status = "failed"
                    break

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
                        if target_idx is None:
                            logger.error(
                                "Gate revise target step not found, failing workflow",
                                extra={
                                    "extra_data": {
                                        "task_id": task.task_id,
                                        "revise_target": revise_target,
                                    }
                                },
                            )
                            state.status = "failed"
                            break
                        state.current_step_index = target_idx
                        # Reset attempt counter so the step gets fresh attempts
                        state.loop_iterations.pop(f"attempts:{revise_target}", None)
                        await self._persist_workflow_state(task)
                        continue
                    # "continue" → advance
                    state.current_step_index += 1
                    await self._persist_workflow_state(task)
                    continue

                # Skip this step if any of its input sources were skipped —
                # running without required input would produce garbage.
                if state.skipped_steps:
                    source_names = resolve_source_names(
                        step_def, state.current_step_index, workflow.steps
                    )
                    missing = [s for s in source_names if s in state.skipped_steps]
                    if missing:
                        logger.warning(
                            "Skipping step — input source was skipped",
                            extra={
                                "extra_data": {
                                    "task_id": task.task_id,
                                    "step": step_def.name,
                                    "missing_sources": missing,
                                }
                            },
                        )
                        state.skipped_steps.append(step_def.name)
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
                    # Step execution failed (e.g. mid-stream LLM error after
                    # internal retries).  Route through _handle_step_retry so
                    # the attempt counter is checked — the step may still have
                    # remaining attempts before exhaustion.
                    logger.warning(
                        "Step execution failed",
                        extra={
                            "extra_data": {
                                "task_id": task.task_id,
                                "step": step_def.name,
                            }
                        },
                    )
                    handled = await self._handle_step_retry(
                        task, step_def, state, workflow, evaluation=None
                    )
                    if not handled:
                        state.status = "failed"
                        break
                    continue

                # Store step output temporarily — will be committed to
                # state.step_outputs only after evaluation approves (or
                # if no evaluation is configured).  This prevents rejected
                # output from being visible to downstream steps.
                pending_output = step_result.model_dump(mode="json")

                # Evaluate if configured
                completion = self._resolve_completion(step_def, workflow)
                if completion and completion.evaluate:
                    evaluation = await self._evaluate_step(
                        step_def, step_result, state, task, workflow
                    )

                    # Persist evaluation and update step_run status based
                    # on the evaluation decision.
                    eval_status = {
                        "approved": "approved",
                        "revise": "rejected",
                        "failed": "failed",
                    }.get(evaluation.decision, "rejected")
                    async with self._session_factory() as db_session:
                        step_run = await get_latest_step_run_for_task_step(
                            db_session, task.task_id, step_def.name
                        )
                        if step_run:
                            await update_step_run(
                                db_session,
                                step_run.step_run_id,
                                evaluation=evaluation.model_dump(mode="json"),
                                status=eval_status,
                            )
                            await db_session.commit()

                    logger.info(
                        "Step evaluation result",
                        extra={
                            "extra_data": {
                                "task_id": task.task_id,
                                "step": step_def.name,
                                "decision": evaluation.decision,
                            }
                        },
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
                        handled = await self._handle_step_retry(
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

                # Step approved or no evaluation — commit the output and advance.
                state.step_outputs[step_def.name] = pending_output

                # Mark the step session as completed now that it's approved.
                if step_result and step_result.session_id:
                    try:
                        await self._session_manager.mark_completed(
                            step_result.session_id,
                            completion_reason="step_approved",
                        )
                    except Exception:
                        logger.warning(
                            "workflow: failed to mark step session completed",
                            extra={
                                "extra_data": {
                                    "session_id": step_result.session_id,
                                }
                            },
                        )

                logger.info(
                    "Step approved, advancing",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "step": step_def.name,
                            "next_step_index": state.current_step_index + 1,
                        }
                    },
                )
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

            # Workflow completed — determine final status.
            # If any steps were skipped due to exhaustion, the task failed.
            state.current_step_status = None
            state.pending_pause_type = None
            state.pending_pause_payload = None

            if state.status == "failed" or state.skipped_steps:
                state.status = "failed"
                task.status = TaskStatus.FAILED
                if state.skipped_steps:
                    skipped = ", ".join(state.skipped_steps)
                    task.result_summary = (
                        f"Workflow failed: steps skipped after exhausting retries ({skipped})"
                    )
                else:
                    task.result_summary = (
                        self._build_result_summary(state, workflow) or "Workflow failed"
                    )
                task.completed_at = datetime.now(UTC)
            else:
                state.status = "completed"
                task.status = TaskStatus.COMPLETED
                task.result_summary = self._build_result_summary(state, workflow)
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

        # Clean up step sessions — mark all as completed/failed based on
        # final task status.  This prevents session resource leaks where
        # step sessions stay "idle" or "active" forever.
        await self._cleanup_step_sessions(task)

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

        # Session handling: reuse existing conversation on retry OR when a
        # prior step_run exists (gate-revise resets the attempt counter but
        # we still want to reuse the conversation so session logs remain
        # accessible for all attempts).
        has_prior_run = False
        if not is_retry:
            async with self._session_factory() as db_session:
                prior_run = await get_latest_step_run_for_task_step(
                    db_session, task.task_id, step_def.name
                )
                has_prior_run = prior_run is not None

        if is_retry or has_prior_run:
            conversation, session = await self._reuse_or_create_step_session(task, step_def, agent)
        else:
            conversation, session = await self._create_step_session(task, step_def, agent)

        # For type="full" input, fork the source step's events into the new
        # session so the conversation appears as natural history.  Skipped on
        # retry (the session already has events from the prior attempt).
        if not is_retry and not has_prior_run:
            effective_input = resolve_effective_input(step_def, step_index, workflow.steps)
            if effective_input.type == "full":
                await self._fork_source_events(
                    source_name=effective_input.single_source(),
                    target_session=session,
                    state=state,
                )

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
                conversation_id=conversation.conversation_id,
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
        runtime = await self._resolve_step_runtime(
            agent=agent,
            user_email=task.created_by,
        )

        # Log resolved step input for debugging.  Prior step context is
        # delivered through event forking (type="full") or the step prompt
        # (type="last"/"summary"), not through a separate prior_context.
        effective_input = resolve_effective_input(step_def, step_index, workflow.steps)
        logger.info(
            "workflow: resolved step input",
            extra={
                "extra_data": {
                    "step": step_def.name,
                    "input_type": effective_input.type,
                    "sources": effective_input.source_names(),
                    "available_outputs": list(state.step_outputs.keys()),
                }
            },
        )
        prior_context = None

        # Select execution policy based on agent type.
        # Secondary agents get SECONDARY_POLICY (skip memory, no orchestration).
        if agent.agent_type == "secondary":
            step_policy = SECONDARY_POLICY
            step_orchestration = OrchestrationMode.NONE
        else:
            step_policy = WORKFLOW_POLICY
            step_orchestration = OrchestrationMode.DELEGATE_SYNC_ONLY

        # Build step context — task steps can delegate (sync only)
        ctx = StepContext(
            step_definition=step_def,
            session=session,
            conversation=conversation,
            agent=agent,
            task_id=task.task_id,
            task_title=task.title,
            task_description=task.description,
            task_expected_output=task.expected_output,
            step_run_id=step_run_id,
            policy=step_policy,
            is_retry=is_retry,
            user_message=step_def.prompt.replace("{user_message}", task.description or task.title),
            prior_context=prior_context,
            interaction_mode=workflow.interaction.mode,
            tool_registry=runtime.tool_registry,
            executor_connection=runtime.executor_connection,
            executor_environment=runtime.executor_environment,
            workflow_state=state,
            workflow_steps=workflow.steps,
            step_index=step_index,
            cancel_event=cancel_event,
            orchestration_mode=step_orchestration,
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
            await runtime.cleanup()

        # Enrich output with session metadata
        if output is not None:
            output.completed_at = datetime.now(UTC)
            output.session_id = session.session_id
            output.intaris_session_id = session.intaris_session_id

        # Update StepRun record — a StepOutput with error set is a failure.
        # If evaluation is configured, set status to "evaluating" instead of
        # premature "approved" — the evaluation will set the final status.
        step_failed = output is None or output.error is not None
        if step_failed:
            initial_status = "failed"
        else:
            completion = self._resolve_completion(step_def, workflow)
            initial_status = "evaluating" if (completion and completion.evaluate) else "approved"
        async with self._session_factory() as db_session:
            await update_step_run(
                db_session,
                step_run_id,
                status=initial_status,
                output=output.model_dump(mode="json") if output else None,
                completed_at=datetime.now(UTC),
            )
            await db_session.commit()

        # Mark the step session as idle — the step has finished executing
        # but may be retried if evaluation rejects.  On approval, the
        # session will be marked completed by the main workflow loop.
        try:
            await self._session_manager.mark_idle(session.session_id)
        except Exception:
            logger.warning(
                "workflow: failed to mark step session idle",
                extra={"extra_data": {"session_id": session.session_id}},
            )

        # Return None for failed steps so the workflow engine treats them
        # as failures (retry logic, on_exhausted handling, etc.)
        return None if step_failed else output

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

        # Pause workflow — write status + workflow_state atomically
        state.status = "paused"
        state.current_step_status = "paused"
        pause_id = f"gate_{uuid.uuid4().hex[:12]}"
        gate_options = [opt.model_dump(mode="json") for opt in gate.options]
        state.pending_pause_type = "gate"
        state.pending_pause_payload = {
            "pause_id": pause_id,
            "task_id": task.task_id,
            "step_name": step_def.name,
            "message": gate.message,
            "context": gate_context,
            "options": gate_options,
        }
        task.status = TaskStatus.PAUSED
        await self._persist_workflow_state(task, sync_status=True)

        # Publish TASK_PAUSED so the chat delegation card updates status
        await self._event_bus.publish(
            Event(
                type=EventType.TASK_PAUSED,
                data={
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "conversation_id": task.source_ref,
                },
            )
        )

        # Create the gate via the notification service so it is persisted
        # to DB, resolved to the source conversation, and survives restarts.
        await self._notification_service.create(
            notification_type="gate",
            user_email=task.created_by,
            conversation_id=task.source_ref or "",
            task_id=task.task_id,
            step_name=step_def.name,
            notification_id=pause_id,
            payload={
                "message": gate.message,
                "context": gate_context,
                "options": gate_options,
                "question": gate.message,
            },
        )

        # Trigger a follow-up turn in the source conversation so the agent
        # can explain the pause to the user (why it paused, what the options are).
        if task.source_type == "chat" and task.source_ref:
            delivery_id: str | None = None
            channel_deliverable = False
            delivery_fallback_text: str | None = None
            try:
                async with self._session_factory() as db_session:
                    from cognis.store.queries import (
                        create_channel_delivery_outbox,
                        get_conversation_channel_route,
                    )

                    route = await get_conversation_channel_route(db_session, task.source_ref)
                    if route is not None:
                        channel_type, account_id, chat_id, thread_id, user_email = route
                        delivery_id = f"cdel_{uuid.uuid4().hex[:12]}"
                        delivery_fallback_text = "Background work paused and needs your attention. Please open the conversation for details."
                        await create_channel_delivery_outbox(
                            db_session,
                            delivery_id=delivery_id,
                            user_email=user_email,
                            conversation_id=task.source_ref,
                            session_id=None,
                            source_type="task",
                            source_id=task.task_id,
                            channel_type=channel_type,
                            account_id=account_id,
                            chat_id=chat_id,
                            thread_id=thread_id,
                            fallback_text=delivery_fallback_text,
                            next_attempt_at=datetime.now(UTC) + timedelta(minutes=2),
                        )
                        await db_session.commit()
                        channel_deliverable = True
            except Exception:
                logger.warning(
                    "gate_follow_up: failed to persist channel follow-up delivery intent",
                    extra={
                        "extra_data": {"task_id": task.task_id, "conversation_id": task.source_ref}
                    },
                    exc_info=True,
                )

            await self._event_bus.publish(
                Event(
                    type=EventType.FOLLOW_UP_TURN_REQUESTED,
                    data={
                        "conversation_id": task.source_ref,
                        "task_id": task.task_id,
                        "task_title": task.title,
                        "status": "paused",
                        "result_summary": gate.message,
                        "gate_message": gate.message,
                        "gate_options": gate_options,
                        "delivery_id": delivery_id,
                        "channel_deliverable": channel_deliverable,
                        "delivery_fallback_text": delivery_fallback_text,
                    },
                )
            )

        # Wait for resolution
        try:
            resolution = await self._pause_waiter.wait(pause_id, timeout=3600.0)
            action = resolution.decision
        except TimeoutError:
            logger.warning(
                "Gate timed out after 3600s, defaulting to continue",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                        "pause_id": pause_id,
                    }
                },
            )
            action = "continue"

        GATES_TOTAL.labels(action=action).inc()

        # Resume — write status + workflow_state atomically
        state.status = "running"
        state.current_step_status = "running"
        state.pending_pause_type = None
        state.pending_pause_payload = None
        task.status = TaskStatus.RUNNING
        await self._persist_workflow_state(task, sync_status=True)

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
            logger.warning(
                "Review loop exhausted",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                        "target": on_reject.target,
                        "iterations": current_iterations,
                        "on_exhausted": exhausted_action,
                    }
                },
            )
            return await self._handle_exhausted(
                task,
                step_def,
                state,
                workflow,
                exhausted_action,
                last_error=state.last_evaluation_feedback,
            )

        REVIEW_LOOPS.labels(step_name=step_def.name).inc()
        state.loop_iterations[loop_key] = current_iterations + 1

        logger.info(
            "Review loop iteration",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "target": on_reject.target,
                    "iteration": current_iterations + 1,
                    "max_iterations": on_reject.max_loop_iterations,
                }
            },
        )

        # Jump back to the target step — reset its attempt counter so it
        # gets fresh attempts (same as gate-revise does).
        target_idx = self._find_step_index(workflow, on_reject.target)
        if target_idx is not None:
            state.current_step_index = target_idx
            state.loop_iterations.pop(f"attempts:{on_reject.target}", None)
            await self._persist_workflow_state(task)
            return True

        return False

    async def _handle_step_retry(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        evaluation: StepEvaluation | None = None,
    ) -> bool:
        """Handle step retry — execution failure or evaluation rejection.

        Unified retry handler for both failure modes:
        - ``evaluation=None``: step execution failed (returned None)
        - ``evaluation`` provided: evaluator rejected the output

        When evaluation is provided, records feedback to the step's
        Intaris session so the agent sees it on retry.

        Retries up to ``max_attempts`` before delegating to
        ``_handle_exhausted``.

        Returns True if retry is possible, False if exhausted.
        """
        completion = self._resolve_completion(step_def, workflow)
        max_attempts = completion.max_attempts if completion else 3
        reason = "evaluation_rejected" if evaluation else "execution_failed"

        # Count attempts for this step
        attempt_key = f"attempts:{step_def.name}"
        current_attempts = state.loop_iterations.get(attempt_key, 1)

        if current_attempts >= max_attempts:
            exhausted_action = self._get_on_exhausted(step_def, workflow)
            # Build a human-readable error summary for the exhaustion gate
            error_summary: str | None = None
            if evaluation:
                error_summary = evaluation.feedback or evaluation.reasoning
            elif state.last_evaluation_feedback:
                error_summary = state.last_evaluation_feedback
            logger.warning(
                "Step retry attempts exhausted",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                        "attempts": current_attempts,
                        "max_attempts": max_attempts,
                        "on_exhausted": exhausted_action,
                        "reason": reason,
                    }
                },
            )
            return await self._handle_exhausted(
                task, step_def, state, workflow, exhausted_action, last_error=error_summary
            )

        # Record evaluation feedback if available (so agent sees it on retry)
        if evaluation:
            await self._record_evaluation_feedback(task, step_def, state, evaluation)

        state.loop_iterations[attempt_key] = current_attempts + 1
        logger.info(
            "Step retry",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "attempt": current_attempts + 1,
                    "max_attempts": max_attempts,
                    "reason": reason,
                }
            },
        )
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
        last_error: str | None = None,
    ) -> bool:
        """Handle exhausted attempts/loops.

        Returns True if handled (workflow continues), False if workflow should fail.
        """
        logger.info(
            "Handling exhausted step",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "action": action,
                    "interaction_mode": workflow.interaction.mode,
                }
            },
        )

        if action == "continue":
            # Skip and advance — record the skipped step so the final
            # task status reflects that not all steps succeeded.
            state.skipped_steps.append(step_def.name)
            logger.warning(
                "Step skipped due to exhaustion",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                    }
                },
            )
            state.current_step_index += 1
            await self._persist_workflow_state(task)
            return True

        elif action == "gate":
            if workflow.interaction.mode == "none":
                # Autonomous mode — gate becomes fail
                logger.warning(
                    "Gate action in autonomous mode, failing step",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "step": step_def.name,
                        }
                    },
                )
                return False
            # Create a gate for user decision
            result = await self._handle_gate_step(
                task,
                StepDefinition(
                    name=f"{step_def.name}_exhausted",
                    type="gate",
                    gate=_build_exhaustion_gate(step_def, last_error=last_error),
                ),
                state,
                workflow,
            )
            if result == "continue":
                state.skipped_steps.append(step_def.name)
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
                    # Reset attempt counter so the step gets fresh attempts
                    state.loop_iterations.pop(f"attempts:{revise_target}", None)
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
            logger.info(
                "task_delivery: silent mode, skipping",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            return

        if target_conversation_id is None:
            logger.warning(
                "task_delivery: no target conversation resolved, skipping",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "delivery_mode": delivery_mode,
                        "source_ref": task.source_ref,
                    }
                },
            )
            return

        logger.info(
            "task_delivery: delivering result",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "status": str(task.status),
                    "target_conversation_id": target_conversation_id,
                    "delivery_mode": delivery_mode,
                }
            },
        )

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

        # Record to target conversation's active Intaris session.
        # The Intaris provider already retries internally (exponential
        # backoff), so a failure here means retries were exhausted.
        # We do one additional delayed attempt before giving up.
        delivery_session_id: str | None = None
        for attempt in range(2):
            try:
                async with self._session_factory() as db_session:
                    from cognis.store.queries import (
                        get_conversation,
                        get_latest_active_session_for_conversation,
                        get_session_row,
                    )

                    sess = await get_latest_active_session_for_conversation(
                        db_session, target_conversation_id
                    )
                    if sess is None:
                        conv = await get_conversation(db_session, target_conversation_id)
                        if conv and conv.active_session_id:
                            sess = await get_session_row(db_session, conv.active_session_id)

                    if sess and sess.intaris_session_id:
                        delivery_session_id = sess.session_id
                        await self._providers.guardrails.record_events(
                            session_id=sess.intaris_session_id,
                            events=[event],
                            source="cognis",
                        )
                break  # Success
            except Exception:
                if attempt == 0:
                    logger.warning(
                        "Task result delivery failed, retrying in 2s",
                        extra={"extra_data": {"task_id": task.task_id}},
                    )
                    await asyncio.sleep(2.0)
                else:
                    logger.error(
                        "Task result delivery failed after retry",
                        extra={"extra_data": {"task_id": task.task_id}},
                        exc_info=True,
                    )

        # Publish events for WebSocket delivery and follow-up turn
        logger.info(
            "task_delivery: publishing EventBus events",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "conversation_id": target_conversation_id,
                }
            },
        )
        event_type = EventType.TASK_FAILED
        if task.status == TaskStatus.COMPLETED:
            event_type = EventType.TASK_COMPLETED
        elif task.status == TaskStatus.CANCELLED:
            event_type = EventType.TASK_CANCELLED

        delivery_id: str | None = None
        channel_deliverable = False
        delivery_fallback_text: str | None = None
        try:
            async with self._session_factory() as db_session:
                from cognis.store.queries import (
                    create_channel_delivery_outbox,
                    get_conversation_channel_route,
                )

                route = await get_conversation_channel_route(db_session, target_conversation_id)
                if route is not None:
                    channel_type, account_id, chat_id, thread_id, user_email = route
                    delivery_id = f"cdel_{uuid.uuid4().hex[:12]}"
                    delivery_fallback_text = {
                        TaskStatus.COMPLETED: "Background work completed. I could not deliver the detailed reply, so please open the conversation for the full result.",
                        TaskStatus.FAILED: "Background work failed. I could not deliver the detailed reply, so please open the conversation for details.",
                        TaskStatus.CANCELLED: "Background work was cancelled. I could not deliver the detailed reply, so please open the conversation for details.",
                    }.get(
                        task.status,
                        "Background work status changed. Please open the conversation for details.",
                    )
                    await create_channel_delivery_outbox(
                        db_session,
                        delivery_id=delivery_id,
                        user_email=user_email,
                        conversation_id=target_conversation_id,
                        session_id=delivery_session_id,
                        source_type="task",
                        source_id=task.task_id,
                        channel_type=channel_type,
                        account_id=account_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        fallback_text=delivery_fallback_text,
                        next_attempt_at=datetime.now(UTC) + timedelta(minutes=2),
                    )
                    await db_session.commit()
                    channel_deliverable = True
        except Exception:
            logger.warning(
                "task_delivery: failed to persist channel follow-up delivery intent",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "conversation_id": target_conversation_id,
                    }
                },
                exc_info=True,
            )

        await self._event_bus.publish(
            Event(
                type=event_type,
                data={
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "title": task.title,
                    "conversation_id": target_conversation_id,
                    "result_summary": task.result_summary,
                    "channel_follow_up_delivery_id": delivery_id,
                },
            )
        )
        # Always request a follow-up turn so the agent can process the
        # result even if Intaris recording failed (degraded mode).
        await self._event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": target_conversation_id,
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "agent_id": task.agent_id,
                    "user_email": task.created_by,
                    "status": str(task.status),
                    "result_summary": task.result_summary,
                    "delivery_id": delivery_id,
                    "channel_deliverable": channel_deliverable,
                    "delivery_fallback_text": delivery_fallback_text,
                },
            )
        )

    async def _persist_workflow_state(self, task: TaskModel, *, sync_status: bool = False) -> None:
        """Persist workflow state to DB after a step transition.

        Increments the workflow state version for optimistic concurrency.
        Before writing, checks the current DB status — if the task has
        been cancelled or failed externally (e.g. via API), the persist
        is aborted and ``StepInterrupted`` is raised so the workflow
        engine's exception handler catches it.

        When *sync_status* is ``True``, the task status is also written
        in the same transaction to avoid split-brain between
        ``task.status`` and ``workflow_state.status``.
        """
        if task.workflow_state is None:
            return

        # Check for external mutations (cancel, fail) before overwriting
        db_status = await self._read_task_status(task.task_id)
        if db_status in {TaskStatus.CANCELLED, TaskStatus.FAILED} and task.status not in {
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }:
            logger.warning(
                "Task was externally %s, aborting workflow state persist",
                db_status,
                extra={"extra_data": {"task_id": task.task_id}},
            )
            task.status = db_status
            raise StepInterrupted(task.task_id)

        task.workflow_state.version += 1
        async with self._session_factory() as db_session:
            ok = await update_task_workflow_state(
                db_session,
                task.task_id,
                task.workflow_state.model_dump(mode="json"),
                expected_version=task.workflow_state.version,
            )
            if not ok:
                logger.warning(
                    "Stale workflow state write detected (version conflict)",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "version": task.workflow_state.version,
                        }
                    },
                )
            if sync_status:
                await update_task_status(db_session, task.task_id, task.status)
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

    async def _cleanup_step_sessions(self, task: TaskModel) -> None:
        """Mark all step sessions as completed/failed based on final task status.

        Called at the end of ``execute_workflow`` to prevent session
        resource leaks.  Sessions that are already completed are skipped.
        """
        from cognis.store.queries import list_step_runs_for_task

        completion_reason = (
            "step_approved" if task.status == TaskStatus.COMPLETED else f"task_{task.status}"
        )
        try:
            async with self._session_factory() as db_session:
                step_runs = await list_step_runs_for_task(db_session, task.task_id)

            for sr in step_runs:
                if sr.session_id is None:
                    continue
                with contextlib.suppress(Exception):
                    await self._session_manager.mark_completed(
                        sr.session_id,
                        completion_reason=completion_reason,
                    )
        except Exception:
            logger.warning(
                "workflow: failed to clean up step sessions",
                extra={"extra_data": {"task_id": task.task_id}},
            )

    async def _read_task_status(self, task_id: str) -> TaskStatus:
        """Read the latest persisted task status."""
        from cognis.store.queries import get_task

        async with self._session_factory() as db_session:
            row = await get_task(db_session, task_id)
        if row is None:
            return TaskStatus.FAILED
        return TaskStatus(str(row.status))

    async def _fork_source_events(
        self,
        source_name: str | None,
        target_session: Any,
        state: WorkflowState,
    ) -> None:
        """Copy events from a source step's session into the target session.

        This implements the ``type="full"`` fork behaviour: the new step
        session starts with the source step's conversation as natural
        history.  Events are written to Intaris (durable) and seeded into
        the session cache (avoids a cold load on context assembly).

        Failures are logged but do not block step execution — the step
        prompt still includes a structured summary from
        ``_build_step_prompt`` as a fallback.
        """
        from cognis.core.session_cache import CachedEvent

        if source_name is None:
            return

        raw_output = state.step_outputs.get(source_name, {})
        cognis_session_id = raw_output.get("session_id")
        intaris_session_id = raw_output.get("intaris_session_id")

        # Read source events — try session cache first, then Intaris
        source_events: list[CachedEvent] = []
        if cognis_session_id:
            cache_entry = self._session_cache.get_entry(cognis_session_id)
            if cache_entry is not None and cache_entry.initialized and cache_entry.events:
                source_events = list(cache_entry.events)

        if not source_events and intaris_session_id:
            try:
                event_read = await self._providers.guardrails.read_events(
                    session_id=intaris_session_id,
                    after_seq=0,
                )
                for raw_event in sorted(event_read.events, key=lambda e: int(e.get("seq", 0))):
                    source_events.append(
                        CachedEvent(
                            seq=int(raw_event.get("seq", 0)),
                            type=str(raw_event.get("type", "")),
                            data=dict(raw_event.get("data", {})),
                            source=raw_event.get("source"),
                            ts=raw_event.get("ts"),
                        )
                    )
            except Exception:
                logger.warning(
                    "workflow: failed to read source events for fork",
                    extra={"extra_data": {"source_step": source_name}},
                    exc_info=True,
                )

        if not source_events:
            logger.debug(
                "workflow: no source events to fork",
                extra={"extra_data": {"source_step": source_name}},
            )
            return

        # Write source events to the new Intaris session
        target_intaris_id = target_session.intaris_session_id or target_session.session_id
        session_events = [SessionEvent(type=e.type, data=e.data) for e in source_events]
        try:
            append_result = await self._providers.guardrails.record_events(
                session_id=target_intaris_id,
                events=session_events,
                source="cognis:fork",
            )
            # Seed the session cache so context assembly doesn't need a cold load
            await self._session_cache.seed_events(
                target_session, source_events, append_result.last_seq
            )
            logger.info(
                "workflow: forked source events into step session",
                extra={
                    "extra_data": {
                        "source_step": source_name,
                        "target_session": target_session.session_id,
                        "event_count": len(source_events),
                        "last_seq": append_result.last_seq,
                    }
                },
            )
        except Exception:
            logger.warning(
                "workflow: failed to fork source events into step session",
                extra={"extra_data": {"source_step": source_name}},
                exc_info=True,
            )

    async def _resolve_step_runtime(
        self,
        *,
        agent: AgentDefinition,
        user_email: str,
    ) -> ResolvedStepRuntime:
        """Resolve the tool registry and executor connection for one step/turn."""
        if callable(self._step_runtime_factory):
            return cast(
                ResolvedStepRuntime,
                await self._step_runtime_factory(agent=agent, user_email=user_email),
            )

        return ResolvedStepRuntime(
            tool_registry=self._shared_tool_registry,
            executor_connection=self._shared_executor_connection,
            cleanup=_noop_cleanup,
            executor_environment=None,
        )

    async def _resolve_step_agent(
        self,
        task: TaskModel,
        step_def: StepDefinition,
    ) -> AgentDefinition | None:
        """Resolve which agent runs a step.

        If the step has ``agent_override``, resolve that agent (checking
        the AgentRegistry for system agents first, then DB). Otherwise,
        resolve the task's primary agent.
        """
        from cognis.core.agent_registry import AgentRegistry

        registry = AgentRegistry(self._session_factory)

        if step_def.agent_override:
            override_agent = await registry.get(step_def.agent_override)
            if override_agent is None:
                logger.warning(
                    "agent_override agent not found, falling back to task agent",
                    extra={
                        "extra_data": {
                            "agent_override": step_def.agent_override,
                            "task_id": task.task_id,
                            "step_name": step_def.name,
                        }
                    },
                )
            else:
                return override_agent

        # Default: use the task's primary agent
        return await registry.get(task.agent_id)

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
                from cognis.core.session import _to_conversation_model, _to_session_model
                from cognis.store.queries import get_conversation, get_session_row

                async with self._session_factory() as db_session:
                    session_row = await get_session_row(db_session, prior_run.session_id)
                    if session_row is not None:
                        conv_row = await get_conversation(db_session, session_row.conversation_id)
                        if conv_row is not None:
                            conversation = _to_conversation_model(conv_row)
                            session = _to_session_model(session_row)
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
                exc_info=True,
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


def _build_exhaustion_gate(step_def: StepDefinition, last_error: str | None = None) -> Any:
    """Build a gate config for exhausted step attempts.

    Includes the last error/evaluation feedback so the user knows what
    went wrong.  Offers "Retry step" — the user can cancel the task
    from the task board if they don't want to retry.
    """
    from cognis.models.workflow import GateConfig, GateOption

    max_attempts = step_def.completion.max_attempts if step_def.completion else 3
    message = f"Step '{step_def.name}' has exhausted its retry limit ({max_attempts} attempts)."
    if last_error:
        message += f"\n\nLast failure: {last_error[:500]}"
    message += "\n\nYou can retry or cancel the task from the task board."

    return GateConfig(
        message=message,
        input=[],
        options=[
            GateOption(label="Retry step", action=f"revise({step_def.name})"),
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
