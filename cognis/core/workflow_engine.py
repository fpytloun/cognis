"""Workflow engine — orchestrates direct turns and workflow steps.

Manages the between-step layer: step sequencing, gates, review loops,
evaluation, and pause/resume. Uses the AgentLoop for within-step
execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import (
    CHAT_POLICY,
    CONTROLLER_TOOL_SURFACE_DIRECT_CHAT,
    CONTROLLER_TOOL_SURFACE_TASK_CONTROL,
    SECONDARY_POLICY,
    WORKFLOW_POLICY,
    AgentLoop,
    PauseWaiter,
    PendingPause,
    StepContext,
    StepInterrupted,
    TokenCallback,
    ToolCallCallback,
    ToolOutputChunkCallback,
    ToolResultCallback,
)
from cognis.core.agent_profiles import resolve_agent_profile
from cognis.core.chat_modes import ResolvedChatMode
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import FollowUpMetadata, FollowUpPolicy
from cognis.core.gate_conditions import evaluate_gate_conditions_detailed
from cognis.core.harness_guards import SameTurnToolCallLedger
from cognis.core.project_runtime import build_project_context_message
from cognis.core.runtime import ResolvedStepRuntime, TransientExecutorUnavailable
from cognis.core.session_fork import fork_session_events
from cognis.core.step_evaluator import StepEvaluator, is_evaluator_malfunction
from cognis.core.task_execution import (
    StaleTaskExecutionOwner,
    assert_task_execution_fence,
    current_task_execution_fence,
)
from cognis.core.workflow_prompt import render_context_comment
from cognis.core.workflow_registry import WorkflowRegistry
from cognis.core.workflow_rendering import (
    MAX_CONTEXT_STRING_BYTES,
    MAX_DETERMINISTIC_JUMPS,
    DeterministicOutputConfig,
    WorkflowRenderer,
    build_render_audit_record,
    normalize_deterministic_output,
)
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import AttachmentRef
from cognis.models.deliverable import Deliverable, DeliverableStatus
from cognis.models.session import SessionEvent, with_session_events_turn_id
from cognis.models.task import TaskModel, TaskStatus
from cognis.models.tool import ToolCall, ToolResult
from cognis.models.workflow import (
    CompletionConfig,
    CompletionDeliveryPolicy,
    StepDefinition,
    StepEvaluation,
    StepOutput,
    Workflow,
    WorkflowState,
    merge_session_policies,
    resolve_effective_input,
    resolve_source_names,
)
from cognis.runtime_context import (
    RuntimeAccessContext,
    current_effective_working_directory,
    current_workspace_root,
    scoped_runtime_context,
)
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.queries import (
    create_step_run,
    defer_running_task,
    fail_running_step_runs_for_task,
    get_agent_direct_conversation,
    get_conversation,
    get_conversation_channel_route,
    get_deliverable,
    get_latest_active_conversation_for_agent,
    get_latest_active_conversation_for_channel_account,
    get_latest_approved_deliverable_for_step_run,
    get_latest_approved_step_run_for_task_step,
    get_latest_rejected_deliverable_for_step_run,
    get_latest_step_run_for_task_step,
    get_preferred_channel_account_for_agent,
    get_project,
    list_pending_context_task_comments,
    list_project_sources,
    list_project_workflow_ids,
    list_step_runs_for_task,
    mark_context_task_comment_applied,
    mark_conversation_unread,
    update_deliverable_status,
    update_step_run,
    update_task_status,
    update_task_workflow_state,
)
from cognis.tools.builtin.orchestration import OrchestrationMode

logger = get_logger(__name__)


def _normalize_executor_path(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return os.path.realpath(os.path.expandvars(os.path.expanduser(value.strip())))
    except OSError:
        return value.strip()


def _resolve_execution_paths(
    *,
    workspace_root: str | None,
    working_directory: str | None,
    executor_home: str | None = None,
    executor_cwd: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve execution paths using the same default as executor tools: cwd, then home."""

    resolved_workspace_root = _normalize_executor_path(workspace_root)
    resolved_working_directory = _normalize_executor_path(working_directory)
    if resolved_working_directory is None:
        resolved_working_directory = resolved_workspace_root or _normalize_executor_path(
            executor_cwd
        )
    if resolved_working_directory is None:
        resolved_working_directory = _normalize_executor_path(executor_home)
    if resolved_workspace_root is None:
        resolved_workspace_root = resolved_working_directory
    return resolved_workspace_root, resolved_working_directory


def _resolve_task_execution_paths(
    task: TaskModel,
    *,
    executor_home: str | None = None,
    executor_cwd: str | None = None,
) -> tuple[str | None, str | None]:
    return _resolve_execution_paths(
        workspace_root=task.workspace_root,
        working_directory=task.working_directory,
        executor_home=executor_home,
        executor_cwd=executor_cwd,
    )


def _effective_task_session_policy(task: TaskModel, workflow: Workflow | None) -> dict[str, Any]:
    """Return the merged workflow-default and task session policy."""

    return merge_session_policies(
        getattr(getattr(workflow, "defaults", None), "session_policy", None),
        getattr(task, "session_policy", None),
    )


def _short_evidence_text(value: Any, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def _bounded_utf8(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    return encoded[: max(0, limit - 3)].decode(errors="ignore") + "..."


# Prometheus metrics
WORKFLOWS_TOTAL = Counter(
    "cognis_workflows_total",
    "Workflow executions",
    labelnames=("workflow_name", "status"),
)

DEFAULT_MAX_WORKFLOW_SECONDS = 14400.0
TRANSIENT_EXECUTOR_BACKOFF_SECONDS = 5
TRANSIENT_EXECUTOR_MAX_DEFERRALS = 12
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
DETERMINISTIC_STEPS_TOTAL = Counter(
    "cognis_workflow_deterministic_steps_total",
    "Controller-owned deterministic workflow step executions.",
    labelnames=("step_type", "status"),
)
DETERMINISTIC_STEP_DURATION = Histogram(
    "cognis_workflow_deterministic_step_duration_seconds",
    "Controller-owned deterministic workflow step duration.",
    labelnames=("step_type",),
)

# Callback type for progress notifications
ProgressCallback = TokenCallback


@dataclass(slots=True)
class _DeterministicStepResult:
    output: StepOutput
    step_run_id: str
    step_run_status: str
    next_step_index: int | None = None
    workflow_status: str | None = None
    delivery_mode_override: str | None = None
    error_action: str | None = None
    route_loop_key: str | None = None
    route_loop_iterations: int | None = None
    revision_context: str | None = None


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
        channel_delivery: Any = None,
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
        self._channel_delivery = channel_delivery
        self._follow_up_policy = FollowUpPolicy(
            llm=getattr(providers, "llm", None),
        )

    async def run_direct_turn(
        self,
        *,
        conversation: Any,
        session: Any,
        agent: AgentDefinition,
        user_message: str,
        intention_eligible: bool = True,
        user_message_metadata: dict[str, Any] | None = None,
        contextual_messages: list[dict[str, Any]] | None = None,
        user_attachments: list[AttachmentRef] | None = None,
        attachment_notice: str | None = None,
        attachment_context: str | None = None,
        system_initiated: bool = False,
        follow_up: FollowUpMetadata | None = None,
        on_progress: ProgressCallback | None = None,
        on_thinking: Any | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
        on_tool_progress: Any | None = None,
        on_tool_output_chunk: ToolOutputChunkCallback | None = None,
        on_context_usage: Any | None = None,
        cancel_event: asyncio.Event | None = None,
        bootstrap_wait_for_intention: bool = False,
        turn_id: str | None = None,
        client_message_id: str | None = None,
        chat_mode: ResolvedChatMode | None = None,
        is_retry: bool = False,
        user_message_already_recorded: bool = False,
        user_message_event_seq: int | None = None,
        consume_boundary_batch: Callable[[str], Any] | None = None,
        wait_for_boundary_input: Callable[[int | None], Any] | None = None,
        get_boundary_action_generation: Callable[[], int] | None = None,
        signal_actionable_boundary_event: Callable[[], None] | None = None,
        get_current_assistant_phase: Callable[[], int] | None = None,
        get_assistant_phase_for_tool: Callable[[str], int | None] | None = None,
        same_turn_tool_call_ledger: SameTurnToolCallLedger | None = None,
        execution_fence: Any | None = None,
        recovery_context: str | None = None,
        on_absorbed_append_start: Callable[[str, str], Any] | None = None,
        on_absorbed_persisted: Callable[[str], Any] | None = None,
    ) -> StepOutput | None:
        """Run the hot-path direct workflow through a workflow-engine entrypoint.

        Direct turns stay single-step and do not create Task or StepRun rows,
        but the engine remains the owner of the orchestration entrypoint so
        metrics, runtime resolution, and future hooks stay centralized.
        """
        platform_data = conversation.context.platform_data or {}
        control_task_id = (
            str(platform_data.get("task_id"))
            if platform_data.get("kind") == "task_control" and platform_data.get("task_id")
            else None
        )
        live_task_session_policy: dict[str, Any] | None = None
        if control_task_id is not None:
            from cognis.store.queries import get_task

            async with self._session_factory() as db_session:
                control_task = await get_task(db_session, control_task_id)
            if (
                control_task is None
                or control_task.created_by != session.user_email
                or control_task.control_conversation_id != conversation.conversation_id
            ):
                raise PermissionError("Task control conversation is no longer authorized")
            if isinstance(control_task.session_policy, dict):
                live_task_session_policy = dict(control_task.session_policy)
        access_context = RuntimeAccessContext(
            user_email=session.user_email,
            agent_id=agent.agent_id,
            agent_owner_email=agent.owner_email,
            agent_type=agent.agent_type,
            session_id=getattr(session, "session_id", None),
            conversation_id=conversation.conversation_id,
            task_id=control_task_id,
            step_name=None,
            step_run_id=None,
            parent_session_id=getattr(session, "parent_session_id", None),
            delegation_mode=getattr(session, "delegation_mode", None),
            workflow_step=False,
            interaction_mode="step_requests",
            session_policy=(
                live_task_session_policy
                if control_task_id is not None
                else (
                    platform_data.get("managed_session_policy")
                    if isinstance(platform_data.get("managed_session_policy"), dict)
                    else None
                )
            ),
            control_surface="task_control" if control_task_id else None,
        )
        runtime = await self._resolve_step_runtime(
            agent=agent,
            user_email=session.user_email,
            access_context=access_context,
            conversation_id=conversation.conversation_id,
        )

        continuation_profile_id = None
        if platform_data.get("forked_from") == "task_step":
            raw_profile_id = platform_data.get("step_profile_id")
            if isinstance(raw_profile_id, str) and raw_profile_id.strip():
                continuation_profile_id = raw_profile_id.strip()

        direct_step = StepDefinition(
            name="direct",
            type="run",
            prompt=user_message,
            allow_questions=True,
            # Keep the inline hot path, but apply the shipped direct-chat profile.
            step_profile_id=continuation_profile_id or "system:direct-default",
        )
        ctx = StepContext(
            step_definition=direct_step,
            session=session,
            conversation=conversation,
            agent=agent,
            executor_agent=agent,
            policy=CHAT_POLICY,
            is_retry=is_retry,
            user_message_already_recorded=user_message_already_recorded,
            remember_user_event_seq=user_message_event_seq,
            user_message=user_message,
            intention_eligible=intention_eligible,
            user_message_metadata=user_message_metadata,
            contextual_messages=contextual_messages or [],
            client_message_id=client_message_id,
            user_attachments=user_attachments or [],
            attachment_notice=attachment_notice,
            attachment_context=attachment_context,
            prior_context=(
                [
                    {
                        "role": "system",
                        "content": recovery_context,
                        "_prior_context": True,
                    }
                ]
                if recovery_context
                else None
            ),
            system_initiated=system_initiated,
            follow_up=follow_up,
            interaction_mode="step_requests",
            tool_registry=runtime.tool_registry,
            executor_connection=runtime.executor_connection,
            executor_environment=runtime.executor_environment,
            executor_pool=getattr(runtime, "executor_pool", None),
            active_executor_id=getattr(runtime, "active_executor_id", None),
            runtime_info=runtime.runtime_info or {},
            workspace_root=current_workspace_root.get(),
            working_directory=current_effective_working_directory.get(),
            cancel_event=cancel_event,
            bootstrap_wait_for_intention=bootstrap_wait_for_intention,
            orchestration_mode=OrchestrationMode.FULL,
            turn_id=turn_id,
            chat_mode=chat_mode,
            controller_tool_surface=(
                CONTROLLER_TOOL_SURFACE_TASK_CONTROL
                if control_task_id
                else CONTROLLER_TOOL_SURFACE_DIRECT_CHAT
            ),
            consume_boundary_batch=consume_boundary_batch,
            wait_for_boundary_input=wait_for_boundary_input,
            get_boundary_action_generation=get_boundary_action_generation,
            signal_actionable_boundary_event=signal_actionable_boundary_event,
            get_current_assistant_phase=get_current_assistant_phase,
            get_assistant_phase_for_tool=get_assistant_phase_for_tool,
            same_turn_tool_call_ledger=same_turn_tool_call_ledger or SameTurnToolCallLedger(),
            execution_fence=execution_fence,
            on_absorbed_append_start=on_absorbed_append_start,
            on_absorbed_persisted=on_absorbed_persisted,
        )
        ctx.workspace_root, ctx.working_directory = _resolve_execution_paths(
            workspace_root=ctx.workspace_root,
            working_directory=ctx.working_directory,
            executor_home=getattr(runtime.executor_environment, "home", None),
            executor_cwd=getattr(runtime.executor_environment, "cwd", None),
        )

        try:
            with scoped_runtime_context(
                user_email=session.user_email,
                agent_id=agent.agent_id,
                agent_owner_email=agent.owner_email,
                workspace_root=ctx.workspace_root,
                effective_working_directory=ctx.working_directory,
                executor_environment=runtime.executor_environment,
                access_context=access_context,
            ):
                await self._session_manager.refresh_intaris_session_policy(session)
                return await self._agent_loop.run_step(
                    ctx,
                    on_token=on_progress,
                    on_thinking=on_thinking,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    on_tool_progress=on_tool_progress,
                    on_tool_output_chunk=on_tool_output_chunk,
                    on_context_usage=on_context_usage,
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
        max_workflow_seconds = DEFAULT_MAX_WORKFLOW_SECONDS
        loop_time = asyncio.get_running_loop()
        active_elapsed_seconds = 0.0
        state = task.workflow_state or WorkflowState()
        task.workflow_state = state

        async def _await_with_workflow_budget(awaitable: Any) -> Any:
            nonlocal active_elapsed_seconds
            remaining = max_workflow_seconds - active_elapsed_seconds
            if remaining <= 0:
                raise TimeoutError
            started = loop_time.time()
            try:
                return await asyncio.wait_for(awaitable, timeout=remaining)
            finally:
                active_elapsed_seconds += loop_time.time() - started

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
                if active_elapsed_seconds > max_workflow_seconds:
                    logger.error(
                        "Workflow execution timed out",
                        extra={
                            "extra_data": {
                                "task_id": task.task_id,
                                "elapsed_seconds": active_elapsed_seconds,
                                "max_seconds": max_workflow_seconds,
                            }
                        },
                    )
                    state.status = "failed"
                    break

                step_def = workflow.steps[state.current_step_index]
                state.current_step_status = "running"
                state.routing_skips.pop(step_def.name, None)

                if step_def.type in {"tool_call", "condition", "complete"}:
                    try:
                        deterministic_result = await _await_with_workflow_budget(
                            self._execute_deterministic_step(
                                task,
                                step_def,
                                state,
                                workflow,
                                cancel_event=cancel_event,
                            )
                        )
                    except TransientExecutorUnavailable as exc:
                        deferred = await self._handle_transient_executor_unavailable(
                            task,
                            step_def,
                            state,
                            workflow,
                            exc,
                        )
                        if deferred:
                            return task
                        state.status = "paused"
                        task.status = TaskStatus.PAUSED
                        await self._persist_workflow_state(task, sync_status=True)
                        WORKFLOWS_TOTAL.labels(
                            workflow_name=workflow.name,
                            status=task.status,
                        ).inc()
                        return task
                    state.step_outputs[step_def.name] = deterministic_result.output.model_dump(
                        mode="json"
                    )

                    if deterministic_result.delivery_mode_override is not None:
                        task.delivery = task.delivery.model_copy(
                            update={"mode": deterministic_result.delivery_mode_override}
                        )

                    if deterministic_result.workflow_status is not None:
                        state.status = cast(Any, deterministic_result.workflow_status)
                        state.current_step_index = len(workflow.steps)
                        await self._persist_workflow_state(task)
                        break

                    if deterministic_result.step_run_status == "paused":
                        state.status = "paused"
                        state.current_step_status = "paused"
                        task.status = TaskStatus.PAUSED
                        await self._persist_workflow_state(task, sync_status=True)
                        return task

                    if deterministic_result.step_run_status == "failed":
                        action = deterministic_result.error_action or "fail"
                        if action == "fail":
                            state.status = "failed"
                            await self._persist_workflow_state(task)
                            break
                        if action == "gate":
                            gate_action = await self._pause_for_deterministic_error(
                                task,
                                step_def,
                                state,
                                workflow,
                                deterministic_result.output.error
                                or deterministic_result.output.summary,
                            )
                            if gate_action == "paused":
                                return task
                            if gate_action == "cancel":
                                state.status = "cancelled"
                                break
                            if gate_action == "fail":
                                state.status = "failed"
                                break

                    next_index = deterministic_result.next_step_index
                    if next_index is None:
                        next_index = state.current_step_index + 1
                    if (
                        deterministic_result.route_loop_key is not None
                        and deterministic_result.route_loop_iterations is not None
                    ):
                        state.loop_iterations[deterministic_result.route_loop_key] = (
                            deterministic_result.route_loop_iterations
                        )
                    if deterministic_result.revision_context is not None:
                        state.last_revision_context = deterministic_result.revision_context
                    self._apply_deterministic_route(
                        state,
                        workflow,
                        source_index=state.current_step_index,
                        target_index=next_index,
                        reason=self._deterministic_route_reason(
                            step_def,
                            deterministic_result.output,
                        ),
                    )
                    state.current_step_status = None
                    state.pending_pause_type = None
                    state.pending_pause_payload = None
                    state.current_step_index = next_index
                    await self._persist_workflow_state(task)
                    continue

                if step_def.type == "gate":
                    gate_result = await self._handle_gate_step(task, step_def, state, workflow)
                    if gate_result == "cancel":
                        state.status = "cancelled"
                        state.last_operator_instruction = None
                        break
                    if gate_result == "fail":
                        state.status = "failed"
                        state.last_operator_instruction = None
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
                        if not state.last_revision_context:
                            gate_payload = state.pending_pause_payload or {}
                            context = str(gate_payload.get("context") or "").strip()
                            message = str(gate_payload.get("message") or "").strip()
                            revision_parts = [
                                (
                                    f"Gate '{step_def.name}' requested revision of "
                                    f"step '{revise_target}'."
                                )
                            ]
                            if message:
                                revision_parts.append(f"Gate message: {message}")
                            if context:
                                revision_parts.append(f"Gate context:\n{context}")
                            state.last_revision_context = "\n\n".join(revision_parts)
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
                try:
                    step_execution = await _await_with_workflow_budget(
                        self._execute_run_step(
                            task,
                            step_def,
                            state,
                            workflow,
                            on_progress=on_progress,
                            cancel_event=cancel_event,
                        )
                    )
                except TransientExecutorUnavailable as exc:
                    deferred = await self._handle_transient_executor_unavailable(
                        task,
                        step_def,
                        state,
                        workflow,
                        exc,
                    )
                    if deferred:
                        return task
                    state.status = "paused"
                    task.status = TaskStatus.PAUSED
                    await self._persist_workflow_state(task, sync_status=True)
                    WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status=task.status).inc()
                    return task
                if isinstance(step_execution, tuple):
                    step_result, step_run_id = step_execution
                else:
                    step_result, step_run_id = step_execution, ""

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
                    evaluation = await _await_with_workflow_budget(
                        self._evaluate_step(
                            step_def,
                            step_result,
                            state,
                            task,
                            workflow,
                        )
                    )

                    # Persist evaluation and update step_run status based
                    # on the evaluation decision.
                    eval_status = {
                        "approved": "approved",
                        "revise": "rejected",
                        "failed": "failed",
                    }.get(evaluation.decision, "rejected")
                    async with self._session_factory() as db_session:
                        await assert_task_execution_fence(db_session)
                        await update_step_run(
                            db_session,
                            step_run_id,
                            evaluation=evaluation.model_dump(mode="json"),
                            status=eval_status,
                            deliverable_id=(
                                None
                                if evaluation.decision == "revise"
                                else step_result.deliverable_id
                            ),
                        )
                        if step_result.deliverable_id is not None:
                            if evaluation.decision == "approved":
                                await update_deliverable_status(
                                    db_session,
                                    step_result.deliverable_id,
                                    status=DeliverableStatus.APPROVED,
                                    evaluator_feedback=None,
                                )
                            elif evaluation.decision == "revise":
                                await update_deliverable_status(
                                    db_session,
                                    step_result.deliverable_id,
                                    status=DeliverableStatus.REJECTED,
                                    evaluator_feedback=evaluation.feedback or evaluation.reasoning,
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
                        if is_evaluator_malfunction(evaluation):
                            logger.error(
                                "Evaluator malfunction failed workflow",
                                extra={
                                    "extra_data": {
                                        "task_id": task.task_id,
                                        "step": step_def.name,
                                        "reasoning": evaluation.reasoning,
                                    }
                                },
                            )
                            state.status = "failed"
                            break
                        exhausted_action = self._get_on_exhausted(step_def, workflow)
                        handled = await self._handle_exhausted(
                            task, step_def, state, workflow, exhausted_action
                        )
                        if not handled:
                            state.status = "failed"
                            break
                        continue

                # Step approved or no evaluation — commit the output and advance.
                if step_result.deliverable_id is not None and not (
                    completion and completion.evaluate
                ):
                    async with self._session_factory() as db_session:
                        await assert_task_execution_fence(db_session)
                        await update_deliverable_status(
                            db_session,
                            step_result.deliverable_id,
                            status=DeliverableStatus.APPROVED,
                            evaluator_feedback=None,
                        )
                        await update_step_run(
                            db_session,
                            step_run_id,
                            deliverable_id=step_result.deliverable_id,
                        )
                        await db_session.commit()

                state.step_outputs[step_def.name] = pending_output

                # Keep a continued session idle while a reachable future step
                # still references it. Otherwise close the approved step session.
                if step_result and step_result.session_id:
                    try:
                        if self._session_needed_by_future_reuse(
                            workflow,
                            state,
                            current_step_index=state.current_step_index,
                            current_step_name=step_def.name,
                            session_id=step_result.session_id,
                        ):
                            await self._session_manager.mark_idle(step_result.session_id)
                        else:
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

                await self._event_bus.publish(
                    Event(
                        type=EventType.STEP_COMPLETED,
                        data={
                            "task_id": task.task_id,
                            "step_name": step_def.name,
                            "step_index": state.current_step_index,
                        },
                    )
                )
                cluster_signals = getattr(self, "cluster_signals", None)
                if cluster_signals is not None:
                    await cluster_signals.publish_task_change(task.task_id)

                routed = await self._handle_step_outcome(
                    task, step_def, step_result, state, workflow
                )
                if routed == "failed":
                    state.status = "failed"
                    break
                if routed == "cancelled":
                    state.status = "cancelled"
                    break
                if routed == "routed":
                    if step_result.session_id and not self._session_needed_by_reuse_from_index(
                        workflow,
                        state,
                        start_step_index=state.current_step_index,
                        session_id=step_result.session_id,
                    ):
                        try:
                            await self._session_manager.mark_completed(
                                step_result.session_id,
                                completion_reason="reuse_route_exhausted",
                            )
                        except Exception:
                            logger.warning(
                                "workflow: failed to close routed reuse session",
                                extra={
                                    "extra_data": {
                                        "task_id": task.task_id,
                                        "step_name": step_def.name,
                                        "session_id": step_result.session_id,
                                    }
                                },
                                exc_info=True,
                            )
                    continue

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
                state.last_evaluation_feedback = None
                state.last_retry_reason = None
                state.last_revision_context = None
                state.last_operator_instruction = None
                state.current_step_index += 1
                await self._persist_workflow_state(task)

            # Workflow completed — determine final status.
            # If any steps were skipped due to exhaustion, the task failed.
            state.current_step_status = None
            state.pending_pause_type = None
            state.pending_pause_payload = None
            persisted_delivery_override = self._deterministic_completion_delivery_override(state)
            if persisted_delivery_override is not None:
                task.delivery = task.delivery.model_copy(
                    update={"mode": persisted_delivery_override}
                )

            if state.status == "cancelled":
                task.status = TaskStatus.CANCELLED
                task.result_summary = (
                    self._build_result_summary(state, workflow) or "Workflow cancelled"
                )
                task.completed_at = datetime.now(UTC)
            elif state.status == "failed" or state.skipped_steps:
                state.status = "failed"
                task.status = TaskStatus.FAILED
                failure_summary = self._build_failure_result_summary(state, workflow)
                if state.skipped_steps:
                    skipped = ", ".join(state.skipped_steps)
                    task.result_summary = (
                        f"Workflow failed: steps skipped after exhausting retries ({skipped})"
                    )
                else:
                    task.result_summary = failure_summary or "Workflow failed"
                task.completed_at = datetime.now(UTC)
            else:
                state.status = "completed"
                task.status = TaskStatus.COMPLETED
                task.result_summary = self._build_result_summary(state, workflow)
                task.completed_at = datetime.now(UTC)

            task.result_data = await self._build_result_data(task, state, workflow)
            task.applied_completion_mode, task.applied_completion_reason = (
                self._resolve_applied_completion(task, state)
            )

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
                WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status=task.status).inc()
            else:
                state.status = "paused"
                task.status = TaskStatus.PAUSED
                await self._persist_workflow_state(task)
                WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status=task.status).inc()
        except TimeoutError:
            logger.error(
                "Workflow execution timed out during active work",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            state.status = "failed"
            state.current_step_status = None
            state.pending_pause_type = None
            state.pending_pause_payload = None
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now(UTC)
            task.result_summary = (
                f"Workflow timed out after {int(active_elapsed_seconds)}s of active execution"
            )
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await fail_running_step_runs_for_task(
                    db_session,
                    task.task_id,
                    datetime.now(UTC),
                    final_status="failed",
                )
                await db_session.commit()
            await self._persist_task_final(task)
            WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status="failed").inc()
        except StaleTaskExecutionOwner:
            raise
        except Exception:
            logger.exception(
                "Workflow execution failed",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            state.status = "failed"
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now(UTC)
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await fail_running_step_runs_for_task(
                    db_session,
                    task.task_id,
                    datetime.now(UTC),
                    final_status="failed",
                )
                await db_session.commit()
            await self._persist_task_final(task)
            WORKFLOWS_TOTAL.labels(workflow_name=workflow.name, status="failed").inc()

        # Clean up step sessions — only when the task has truly reached a
        # terminal state. Pausing a task (StepInterrupted) leaves
        # ``task.status == running`` and keeps the in-flight step session
        # in place so it can resume. Running cleanup on pause would mark
        # the session ``completed``, forcing a fresh session on resume
        # and losing the pending tool call context.
        is_terminal = task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        if self._notification_service is not None and is_terminal:
            await self._notification_service.mark_task_notifications_terminal(
                task.task_id,
                reason=f"task_{task.status}",
            )
        if is_terminal:
            await self._cleanup_step_sessions(task)

        duration = (datetime.now(UTC) - start_time).total_seconds()
        WORKFLOW_DURATION.labels(workflow_name=workflow.name).observe(duration)

        # Deliver result
        if is_terminal:
            await self._deliver_task_result(task)

        return task

    async def _execute_deterministic_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        *,
        cancel_event: asyncio.Event | None,
    ) -> _DeterministicStepResult:
        """Execute or recover one controller-owned deterministic step."""

        started = asyncio.get_running_loop().time()
        generation = state.loop_iterations.get(f"deterministic_generation:{step_def.name}", 0)
        step_run_id: str
        runtime_info: dict[str, Any]
        latest = None
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            latest = await get_latest_step_run_for_task_step(
                db_session,
                task.task_id,
                step_def.name,
                attempt_number=task.attempt_number,
                current_revision_only=True,
            )
            latest_runtime = (
                dict(latest.runtime_info)
                if latest is not None and isinstance(latest.runtime_info, dict)
                else {}
            )
            if latest is not None and latest_runtime.get("deterministic_generation") == generation:
                step_run_id = latest.step_run_id
                runtime_info = latest_runtime
            else:
                step_run_id = f"sr_{uuid.uuid4().hex}"
                runtime_info = {
                    "deterministic_step": True,
                    "deterministic_substate": "rendering",
                    "deterministic_generation": generation,
                }
                await create_step_run(
                    db_session,
                    task_id=task.task_id,
                    step_name=step_def.name,
                    step_type=step_def.type,
                    agent_id=task.agent_id,
                    attempt=generation + 1,
                    attempt_number=task.attempt_number,
                    step_run_id=step_run_id,
                    workspace_root=task.workspace_root,
                    working_directory=task.working_directory,
                    runtime_info=runtime_info,
                    status="pending",
                    started_at=datetime.now(UTC),
                )
                await update_step_run(
                    db_session,
                    step_run_id,
                    status="running",
                    runtime_info=runtime_info,
                )
                await db_session.commit()
                latest = None

        try:
            if latest is not None:
                recovered = await self._recover_deterministic_step(
                    latest,
                    step_def=step_def,
                    workflow=workflow,
                )
                if recovered is not None:
                    DETERMINISTIC_STEPS_TOTAL.labels(
                        step_type=step_def.type,
                        status=f"recovered_{recovered.step_run_status}",
                    ).inc()
                    return recovered

            await self._event_bus.publish(
                Event(
                    type=EventType.STEP_STARTED,
                    data={
                        "task_id": task.task_id,
                        "step_name": step_def.name,
                        "step_run_id": step_run_id,
                        "deterministic_step": True,
                    },
                )
            )

            renderer = WorkflowRenderer()
            context = self._deterministic_render_context(task, state, workflow, step_def)
            if step_def.when is not None:
                when_result = renderer.render_expression(step_def.when, context)
                runtime_info["when"] = build_render_audit_record(
                    template=step_def.when,
                    rendered=when_result,
                )
                await self._persist_deterministic_checkpoint(
                    step_run_id,
                    runtime_info=runtime_info,
                )
                if not when_result:
                    output_config = step_def.on_skip or DeterministicOutputConfig(
                        summary=f"Skipped deterministic step '{step_def.name}'.",
                        metadata={"skipped": True, "skip_reason": "when_false"},
                    )
                    output = normalize_deterministic_output(
                        output_config,
                        renderer,
                        context,
                        step_type=step_def.type,
                    )
                    output.metadata.update({"skipped": True, "skip_reason": "when_false"})
                    return await self._finalize_deterministic_step(
                        task,
                        step_def,
                        step_run_id,
                        runtime_info,
                        output,
                        status="skipped",
                        next_step_index=self._explicit_next_index(
                            workflow,
                            step_def.next,
                            state.current_step_index + 1,
                        ),
                    )

            if step_def.type == "condition":
                return await self._execute_condition_step(
                    task,
                    step_def,
                    state,
                    workflow,
                    step_run_id,
                    runtime_info,
                    renderer,
                    context,
                )
            if step_def.type == "complete":
                return await self._execute_complete_step(
                    task,
                    step_def,
                    step_run_id,
                    runtime_info,
                    renderer,
                    context,
                )
            return await self._execute_tool_call_step(
                task,
                step_def,
                state,
                workflow,
                step_run_id,
                runtime_info,
                renderer,
                context,
                cancel_event=cancel_event,
            )
        except (StaleTaskExecutionOwner, StepInterrupted, TransientExecutorUnavailable):
            raise
        except Exception as exc:
            logger.exception(
                "workflow: deterministic step failed",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                        "step_type": step_def.type,
                    }
                },
            )
            return await self._fail_deterministic_step(
                task,
                step_def,
                step_run_id,
                runtime_info,
                str(exc) or exc.__class__.__name__,
            )
        finally:
            DETERMINISTIC_STEP_DURATION.labels(step_type=step_def.type).observe(
                asyncio.get_running_loop().time() - started
            )

    async def _recover_deterministic_step(
        self,
        latest: Any,
        *,
        step_def: StepDefinition,
        workflow: Workflow,
    ) -> _DeterministicStepResult | None:
        runtime_info = dict(latest.runtime_info or {})
        substate = runtime_info.get("deterministic_substate")
        terminal_statuses = {"approved", "skipped", "failed", "paused"}
        if latest.status in terminal_statuses and isinstance(latest.output, dict):
            return self._deterministic_result_from_persisted(
                latest.step_run_id,
                latest.status,
                latest.output,
                runtime_info,
                workflow,
            )
        if substate == "persisted" and isinstance(latest.output, dict):
            terminal_status = str(runtime_info.get("terminal_status") or "approved")
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    latest.step_run_id,
                    status=terminal_status,
                    completed_at=datetime.now(UTC),
                )
                await db_session.commit()
            return self._deterministic_result_from_persisted(
                latest.step_run_id,
                terminal_status,
                latest.output,
                runtime_info,
                workflow,
            )
        if substate == "executing" and runtime_info.get("tool_read_only") is not True:
            return await self._fail_deterministic_step(
                SimpleNamespace(task_id=latest.task_id),
                step_def,
                latest.step_run_id,
                runtime_info,
                (
                    "Deterministic tool dispatch outcome is ambiguous after restart; "
                    "the side-effecting or unknown tool was not replayed."
                ),
            )
        return None

    def _deterministic_result_from_persisted(
        self,
        step_run_id: str,
        status: str,
        raw_output: dict[str, Any],
        runtime_info: dict[str, Any],
        workflow: Workflow,
    ) -> _DeterministicStepResult:
        exhausted = runtime_info.get("condition_exhausted") is True
        target_name = runtime_info.get("selected_target")
        next_index = runtime_info.get("next_step_index")
        if not exhausted and isinstance(target_name, str):
            next_index = self._find_step_index(workflow, target_name)
        return _DeterministicStepResult(
            output=StepOutput.model_validate(raw_output),
            step_run_id=step_run_id,
            step_run_status=status,
            next_step_index=next_index if isinstance(next_index, int) else None,
            workflow_status=(
                str(runtime_info["workflow_status"])
                if runtime_info.get("workflow_status") in {"completed", "failed"}
                else None
            ),
            delivery_mode_override=(
                str(runtime_info["delivery_mode_override"])
                if isinstance(runtime_info.get("delivery_mode_override"), str)
                else None
            ),
            error_action=(
                str(runtime_info["error_action"])
                if isinstance(runtime_info.get("error_action"), str)
                else None
            ),
            route_loop_key=(
                str(runtime_info["route_loop_key"])
                if isinstance(runtime_info.get("route_loop_key"), str)
                else None
            ),
            route_loop_iterations=(
                int(runtime_info["route_loop_iterations"])
                if isinstance(runtime_info.get("route_loop_iterations"), int)
                else None
            ),
            revision_context=(
                str(runtime_info["revision_context"])
                if isinstance(runtime_info.get("revision_context"), str)
                else None
            ),
        )

    async def _execute_condition_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        step_run_id: str,
        runtime_info: dict[str, Any],
        renderer: WorkflowRenderer,
        context: dict[str, Any],
    ) -> _DeterministicStepResult:
        assert step_def.condition is not None
        selected = renderer.render_expression(step_def.condition.if_, context)
        target_name = step_def.condition.then if selected else step_def.condition.else_
        next_index = self._explicit_next_index(
            workflow,
            target_name,
            state.current_step_index + 1,
        )
        if (
            target_name is not None
            and next_index <= state.current_step_index
            and step_def.condition.max_loop_iterations is not None
        ):
            loop_key = f"condition:{step_def.name}->{target_name}"
            current_iterations = state.loop_iterations.get(loop_key, 0)
            if current_iterations >= step_def.condition.max_loop_iterations:
                runtime_info.update(
                    {
                        "selected_branch": "exhausted",
                        "selected_target": target_name,
                        "condition_exhausted": True,
                        "next_step_index": state.current_step_index + 1,
                        "loop_key": loop_key,
                        "loop_iterations": current_iterations,
                        "max_loop_iterations": step_def.condition.max_loop_iterations,
                    }
                )
                return await self._fail_deterministic_step(
                    task,
                    step_def,
                    step_run_id,
                    runtime_info,
                    (
                        f"Condition route '{step_def.name}' exhausted after "
                        f"{current_iterations} backward iterations."
                    ),
                    action_override=step_def.condition.on_exhausted,
                )
            runtime_info.update(
                {
                    "route_loop_key": loop_key,
                    "route_loop_iterations": current_iterations + 1,
                }
            )
        if (
            target_name is not None
            and next_index <= state.current_step_index
            and step_def.condition.revision_source is not None
        ):
            revision_source = step_def.condition.revision_source
            source_index = self._find_step_index(workflow, revision_source)
            source_payload = state.step_outputs.get(revision_source)
            if source_index is None or not isinstance(source_payload, dict):
                raise ValueError(
                    f"condition revision_source {revision_source!r} has no completed output"
                )
            source_output = StepOutput.model_validate(source_payload)
            runtime_info["revision_context"] = self._build_revision_context(
                workflow.steps[source_index],
                source_output,
            )
        branch = "then" if selected else "else"
        output_config = step_def.condition.output or DeterministicOutputConfig(
            summary=f"Condition '{step_def.name}' selected the {branch} branch.",
        )
        output = normalize_deterministic_output(
            output_config,
            renderer,
            context,
            step_type=step_def.type,
        )
        output.metadata.update(
            {
                "condition_result": selected,
                "selected_branch": branch,
                "selected_target": target_name,
            }
        )
        runtime_info.update(
            {
                "condition": build_render_audit_record(
                    template=step_def.condition.if_,
                    rendered=selected,
                ),
                "selected_branch": branch,
                "selected_target": target_name,
                "next_step_index": next_index,
            }
        )
        return await self._finalize_deterministic_step(
            task,
            step_def,
            step_run_id,
            runtime_info,
            output,
            status="approved",
            next_step_index=next_index,
        )

    async def _execute_complete_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        step_run_id: str,
        runtime_info: dict[str, Any],
        renderer: WorkflowRenderer,
        context: dict[str, Any],
    ) -> _DeterministicStepResult:
        assert step_def.complete is not None
        complete = step_def.complete
        notification = complete.notification
        if complete.delivery_mode_override == "silent" and notification is None:
            notification = {
                "mode": "silent",
                "reason": renderer.render_text(complete.summary, context),
            }
        output = StepOutput(
            summary=renderer.render_text(complete.summary, context),
            content=renderer.render_text(complete.content, context) if complete.content else "",
            outputs=renderer.render_native(complete.outputs, context),
            metadata={
                "deterministic_step": True,
                "step_type": step_def.type,
                "completion_status": complete.status,
                "delivery_mode_override": complete.delivery_mode_override,
            },
            notification=notification,
        )
        runtime_info.update(
            {
                "workflow_status": complete.status,
                "delivery_mode_override": complete.delivery_mode_override,
            }
        )
        return await self._finalize_deterministic_step(
            task,
            step_def,
            step_run_id,
            runtime_info,
            output,
            status="approved",
            workflow_status=complete.status,
            delivery_mode_override=complete.delivery_mode_override,
        )

    async def _execute_tool_call_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        step_run_id: str,
        runtime_info: dict[str, Any],
        renderer: WorkflowRenderer,
        context: dict[str, Any],
        *,
        cancel_event: asyncio.Event | None,
    ) -> _DeterministicStepResult:
        assert step_def.tool_call is not None
        config = step_def.tool_call
        rendered_args = renderer.render_native(config.args, context)
        if not isinstance(rendered_args, dict):
            raise ValueError("deterministic tool_call args must render to an object")
        call_identity = self._deterministic_call_identity(
            task,
            step_def,
            step_run_id,
        )
        runtime_info.update(
            {
                "call_identity": call_identity,
                "tool_name": config.tool,
                "render": build_render_audit_record(
                    template=config.args,
                    rendered=rendered_args,
                    redact_keys=set(config.redact_args),
                ),
            }
        )
        await self._persist_deterministic_checkpoint(
            step_run_id,
            runtime_info=runtime_info,
        )

        primary_agent, agent = await self._resolve_step_agents(task, step_def)
        if primary_agent is None or agent is None:
            raise RuntimeError("could not resolve task agent for deterministic tool_call")
        profile = resolve_agent_profile(
            agent,
            task.agent_profile_id,
            source="task",
        )
        session_policy = _effective_task_session_policy(task, workflow)
        conversation, session, _ = await self._reuse_or_create_step_session(
            task,
            step_def,
            agent,
            agent_profile_id=profile.profile_id,
            session_policy=session_policy,
        )
        executor_agent = self._executor_agent_for_step(primary_agent, agent)
        access_context = RuntimeAccessContext(
            user_email=task.created_by,
            agent_id=executor_agent.agent_id,
            agent_owner_email=executor_agent.owner_email,
            agent_type=executor_agent.agent_type,
            session_id=session.session_id,
            conversation_id=conversation.conversation_id,
            task_id=task.task_id,
            step_name=step_def.name,
            step_run_id=step_run_id,
            parent_session_id=session.parent_session_id,
            delegation_mode=session.delegation_mode,
            workflow_step=True,
            interaction_mode=task.interaction_mode_override or workflow.interaction.mode,
            session_policy=session_policy,
        )
        runtime = await self._resolve_step_runtime(
            agent=agent,
            executor_agent=executor_agent,
            user_email=task.created_by,
            access_context=access_context,
            conversation_id=conversation.conversation_id,
            task_id=task.task_id,
        )
        try:
            registry = runtime.tool_registry
            registered = registry.get(config.tool) if registry is not None else None
            if registered is None:
                raise ValueError(f"deterministic tool_call references unknown tool {config.tool!r}")
            read_only = registered.definition.read_only is True
            runtime_info.update(
                {
                    **(runtime.runtime_info or {}),
                    **profile.audit_metadata(),
                    "tool_read_only": read_only,
                    "allow_side_effects": config.allow_side_effects,
                    "deterministic_substate": "executing",
                    "session_id": session.session_id,
                    "intaris_session_id": session.intaris_session_id,
                }
            )
            if not read_only and not config.allow_side_effects:
                raise PermissionError(
                    "deterministic tool_call blocked because the tool is write-capable "
                    "or lacks explicit read-only metadata; set allow_side_effects=true "
                    "only for an intentionally authorized workflow"
                )
            await self._persist_deterministic_checkpoint(
                step_run_id,
                runtime_info=runtime_info,
                session_id=session.session_id,
                intaris_session_id=session.intaris_session_id,
            )

            ctx = StepContext(
                step_definition=step_def,
                session=session,
                conversation=conversation,
                agent=agent,
                executor_agent=executor_agent,
                task_id=task.task_id,
                task_title=task.title,
                task_description=task.description,
                task_expected_output=task.expected_output,
                completion_delivery=task.completion_delivery,
                step_run_id=step_run_id,
                policy=WORKFLOW_POLICY,
                interaction_mode=task.interaction_mode_override or workflow.interaction.mode,
                session_policy=session_policy,
                tool_registry=registry,
                executor_connection=runtime.executor_connection,
                executor_environment=runtime.executor_environment,
                executor_pool=getattr(runtime, "executor_pool", None),
                active_executor_id=getattr(runtime, "active_executor_id", None),
                runtime_info=runtime.runtime_info or {},
                workflow_state=state,
                workflow_steps=workflow.steps,
                step_index=state.current_step_index,
                cancel_event=cancel_event,
                system_initiated=task.source_type in {"scheduler", "webhook"},
                orchestration_mode=OrchestrationMode.NONE,
                turn_id=call_identity,
                execution_fence=current_task_execution_fence(),
            )
            ctx.workspace_root, ctx.working_directory = _resolve_execution_paths(
                workspace_root=task.workspace_root,
                working_directory=task.working_directory,
                executor_home=getattr(runtime.executor_environment, "home", None),
                executor_cwd=getattr(runtime.executor_environment, "cwd", None),
            )
            tool_call = ToolCall(
                call_id=call_identity,
                name=config.tool,
                arguments=rendered_args,
                execution_scope_id=step_run_id,
            )
            with scoped_runtime_context(
                user_email=task.created_by,
                agent_id=agent.agent_id,
                agent_owner_email=agent.owner_email,
                workspace_root=ctx.workspace_root,
                effective_working_directory=ctx.working_directory,
                executor_environment=runtime.executor_environment,
                access_context=access_context,
            ):
                dispatch = self._agent_loop.execute_controller_tool(ctx, tool_call)
                result: ToolResult
                if config.timeout_seconds is not None:
                    result = await asyncio.wait_for(dispatch, timeout=config.timeout_seconds)
                else:
                    result = await dispatch
                raw_output = (
                    result.metadata.get("_raw_output")
                    if isinstance(result.metadata, dict)
                    else None
                )
                result = await self._agent_loop.persist_controller_tool_output(
                    ctx,
                    tool_call,
                    result,
                )

            result_context = self._deterministic_tool_result_context(
                result,
                raw_output=raw_output,
            )
            rendered_result_context = {**context, "result": result_context}
            outputs = (
                renderer.render_native(config.outputs, rendered_result_context)
                if config.outputs
                else self._default_deterministic_tool_outputs(result_context, result)
            )
            summary_template = config.summary or (
                f"Tool '{config.tool}' failed."
                if result.is_error
                else f"Tool '{config.tool}' completed."
            )
            output = StepOutput(
                summary=renderer.render_text(summary_template, rendered_result_context),
                outputs=outputs,
                metadata={
                    "deterministic_step": True,
                    "step_type": step_def.type,
                    "tool_name": config.tool,
                    "call_identity": call_identity,
                    "tool_error": result.is_error,
                    "output_ref": (result.metadata or {}).get("recovery_call_id"),
                    "tool_output_artifact_id": (result.metadata or {}).get(
                        "tool_output_artifact_id"
                    ),
                    "target_executor_id": (
                        rendered_args.get("target_executor")
                        if isinstance(rendered_args.get("target_executor"), str)
                        else None
                    ),
                },
                error=result.output if result.is_error else None,
                attachments=result.attachments or [],
            )
            runtime_info.update(
                {
                    "tool_error": result.is_error,
                    "output_ref": (result.metadata or {}).get("recovery_call_id"),
                    "tool_output_artifact_id": (result.metadata or {}).get(
                        "tool_output_artifact_id"
                    ),
                    "selected_executor_id": (result.metadata or {}).get("executor_id"),
                    "next_step_index": self._explicit_next_index(
                        workflow,
                        step_def.next,
                        state.current_step_index + 1,
                    ),
                }
            )
            if result.is_error and config.fail_on_error:
                return await self._fail_deterministic_step(
                    task,
                    step_def,
                    step_run_id,
                    runtime_info,
                    result.output,
                    output=output,
                )
            return await self._finalize_deterministic_step(
                task,
                step_def,
                step_run_id,
                runtime_info,
                output,
                status="approved",
                next_step_index=int(runtime_info["next_step_index"]),
            )
        except TimeoutError:
            runtime_info["dispatch_timeout"] = True
            runtime_info["ambiguous_side_effect"] = runtime_info.get("tool_read_only") is not True
            return await self._fail_deterministic_step(
                task,
                step_def,
                step_run_id,
                runtime_info,
                "Deterministic tool execution timed out.",
            )
        finally:
            await runtime.cleanup()

    async def _fail_deterministic_step(
        self,
        task: Any,
        step_def: StepDefinition,
        step_run_id: str,
        runtime_info: dict[str, Any],
        error: str,
        *,
        output: StepOutput | None = None,
        action_override: str | None = None,
    ) -> _DeterministicStepResult:
        bounded_error = " ".join(str(error).split())[:1000] or "Deterministic step failed."
        action = action_override or step_def.on_error or "fail"
        output = output or StepOutput(
            summary=f"Deterministic step '{step_def.name}' failed.",
            outputs={},
            metadata={
                "deterministic_step": True,
                "step_type": step_def.type,
                "error_action": action,
            },
            error=bounded_error,
        )
        output.metadata.update(
            {
                "error_action": action,
                "skipped": action == "skip",
                "skip_reason": "on_error" if action == "skip" else None,
            }
        )
        runtime_info.update({"error": bounded_error, "error_action": action})
        status = "skipped" if action == "skip" else "failed"
        return await self._finalize_deterministic_step(
            task,
            step_def,
            step_run_id,
            runtime_info,
            output,
            status=status,
            error_action=action,
        )

    async def _finalize_deterministic_step(
        self,
        task: Any,
        step_def: StepDefinition,
        step_run_id: str,
        runtime_info: dict[str, Any],
        output: StepOutput,
        *,
        status: str,
        next_step_index: int | None = None,
        workflow_status: str | None = None,
        delivery_mode_override: str | None = None,
        error_action: str | None = None,
    ) -> _DeterministicStepResult:
        effective_next_step_index = (
            next_step_index
            if next_step_index is not None
            else (
                int(runtime_info["next_step_index"])
                if isinstance(runtime_info.get("next_step_index"), int)
                else None
            )
        )
        runtime_info.update(
            {
                "deterministic_substate": "persisted",
                "terminal_status": status,
                "next_step_index": effective_next_step_index,
                "workflow_status": workflow_status,
                "delivery_mode_override": delivery_mode_override,
                "error_action": error_action,
                "output_refs": {
                    "step_output": step_def.name,
                    "tool_call_id": output.metadata.get("call_identity"),
                    "tool_output_ref": output.metadata.get("output_ref"),
                    "artifact_id": output.metadata.get("tool_output_artifact_id"),
                },
            }
        )
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            await update_step_run(
                db_session,
                step_run_id,
                output=output.model_dump(mode="json"),
                runtime_info=runtime_info,
            )
            await db_session.commit()
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            await update_step_run(
                db_session,
                step_run_id,
                status=status,
                completed_at=datetime.now(UTC),
            )
            await db_session.commit()

        await self._event_bus.publish(
            Event(
                type=EventType.STEP_COMPLETED,
                data={
                    "task_id": task.task_id,
                    "step_name": step_def.name,
                    "step_run_id": step_run_id,
                    "status": status,
                    "deterministic_step": True,
                },
            )
        )
        DETERMINISTIC_STEPS_TOTAL.labels(step_type=step_def.type, status=status).inc()
        logger.info(
            "workflow: deterministic step completed",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "step_type": step_def.type,
                    "status": status,
                    "selected_branch": runtime_info.get("selected_branch"),
                    "selected_target": runtime_info.get("selected_target"),
                    "output_ref": runtime_info.get("output_ref"),
                    "error_action": error_action,
                }
            },
        )
        return _DeterministicStepResult(
            output=output,
            step_run_id=step_run_id,
            step_run_status=status,
            next_step_index=effective_next_step_index,
            workflow_status=workflow_status,
            delivery_mode_override=delivery_mode_override,
            error_action=error_action,
            route_loop_key=(
                str(runtime_info["route_loop_key"])
                if isinstance(runtime_info.get("route_loop_key"), str)
                else None
            ),
            route_loop_iterations=(
                int(runtime_info["route_loop_iterations"])
                if isinstance(runtime_info.get("route_loop_iterations"), int)
                else None
            ),
            revision_context=(
                str(runtime_info["revision_context"])
                if isinstance(runtime_info.get("revision_context"), str)
                else None
            ),
        )

    async def _persist_deterministic_checkpoint(
        self,
        step_run_id: str,
        *,
        runtime_info: dict[str, Any],
        session_id: str | None = None,
        intaris_session_id: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"runtime_info": runtime_info}
        if session_id is not None:
            kwargs["session_id"] = session_id
        if intaris_session_id is not None:
            kwargs["intaris_session_id"] = intaris_session_id
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            await update_step_run(db_session, step_run_id, **kwargs)
            await db_session.commit()

    def _deterministic_render_context(
        self,
        task: TaskModel,
        state: WorkflowState,
        workflow: Workflow,
        step_def: StepDefinition,
    ) -> dict[str, Any]:
        steps: dict[str, Any] = {}
        for name, raw in state.step_outputs.items():
            if not isinstance(raw, dict):
                continue
            steps[name] = {
                "summary": raw.get("summary", ""),
                "content": raw.get("content", ""),
                "outputs": raw.get("outputs", {}),
                "metadata": raw.get("metadata", {}),
                "status": "skipped" if raw.get("metadata", {}).get("skipped") else "completed",
                "error": raw.get("error"),
                "deliverable_id": raw.get("deliverable_id"),
                "attachments": raw.get("attachments", []),
            }
        task_vars = (
            task.result_data.get("vars", {})
            if isinstance(task.result_data, dict) and isinstance(task.result_data.get("vars"), dict)
            else {}
        )
        return {
            "task": {
                "id": task.task_id,
                "title": task.title,
                "description": task.description,
                "expected_output": task.expected_output,
                "source_type": task.source_type,
                "source_ref": task.source_ref,
                "attempt_number": task.attempt_number,
            },
            "workflow": {
                "id": workflow.workflow_id,
                "name": workflow.name,
                "version": workflow.version,
                "step": step_def.name,
            },
            "vars": task_vars,
            "thresholds": {},
            "steps": steps,
        }

    @staticmethod
    def _deterministic_call_identity(
        task: TaskModel,
        step_def: StepDefinition,
        step_run_id: str,
    ) -> str:
        value = f"{task.task_id}:{step_def.name}:{task.attempt_number}:{step_run_id}".encode()
        return f"det_{hashlib.sha256(value).hexdigest()[:32]}"

    @staticmethod
    def _deterministic_tool_result_context(
        result: ToolResult,
        *,
        raw_output: Any,
    ) -> dict[str, Any]:
        raw_text = raw_output if isinstance(raw_output, str) else result.output
        value: Any = raw_text
        if len(raw_text.encode()) <= MAX_CONTEXT_STRING_BYTES:
            try:
                value = json.loads(raw_text)
            except (TypeError, ValueError):
                value = raw_text
        else:
            value = _bounded_utf8(raw_text, MAX_CONTEXT_STRING_BYTES)
        metadata = {
            key: item
            for key, item in (result.metadata or {}).items()
            if key not in {"_raw_output", "stored_output", "evaluation"}
        }
        return {
            "output": _bounded_utf8(result.output, MAX_CONTEXT_STRING_BYTES),
            "value": value,
            "is_error": result.is_error,
            "metadata": metadata,
            "attachments": result.attachments or [],
        }

    @staticmethod
    def _default_deterministic_tool_outputs(
        result_context: dict[str, Any],
        result: ToolResult,
    ) -> dict[str, Any]:
        value = result_context["value"]
        outputs = dict(value) if isinstance(value, dict) else {"result": value}
        recovery_call_id = (result.metadata or {}).get("recovery_call_id")
        if isinstance(recovery_call_id, str):
            outputs["tool_output_ref"] = recovery_call_id
        artifact_id = (result.metadata or {}).get("tool_output_artifact_id")
        if isinstance(artifact_id, str):
            outputs["tool_output_artifact_id"] = artifact_id
        return outputs

    def _explicit_next_index(
        self,
        workflow: Workflow,
        target_name: str | None,
        fallback: int,
    ) -> int:
        if target_name is None:
            return fallback
        target = self._find_step_index(workflow, target_name)
        if target is None:
            raise ValueError(f"unknown deterministic routing target {target_name!r}")
        return target

    def _apply_deterministic_route(
        self,
        state: WorkflowState,
        workflow: Workflow,
        *,
        source_index: int,
        target_index: int,
        reason: str,
    ) -> None:
        if target_index == source_index + 1:
            return
        jumps = state.loop_iterations.get("deterministic_jumps", 0) + 1
        state.loop_iterations["deterministic_jumps"] = jumps
        if jumps > MAX_DETERMINISTIC_JUMPS:
            raise RuntimeError("deterministic workflow jump limit exceeded")
        if target_index > source_index:
            for step in workflow.steps[source_index + 1 : target_index]:
                state.routing_skips[step.name] = reason[:500]
                state.step_outputs.pop(step.name, None)
            return
        target_name = workflow.steps[target_index].name
        state.last_retry_reason = "routed_revision"
        attempt_key = f"attempts:{target_name}"
        state.loop_iterations[attempt_key] = max(state.loop_iterations.get(attempt_key, 1), 1) + 1
        for index in range(target_index, source_index + 1):
            name = workflow.steps[index].name
            state.routing_skips.pop(name, None)
            state.step_outputs.pop(name, None)
            generation_key = f"deterministic_generation:{name}"
            state.loop_iterations[generation_key] = state.loop_iterations.get(generation_key, 0) + 1

    @staticmethod
    def _deterministic_route_reason(
        step_def: StepDefinition,
        output: StepOutput,
    ) -> str:
        branch = output.metadata.get("selected_branch")
        if isinstance(branch, str):
            return f"condition:{step_def.name}:{branch}"[:500]
        if output.metadata.get("skipped"):
            return f"when:{step_def.name}:false"[:500]
        return f"next:{step_def.name}"[:500]

    async def _pause_for_deterministic_error(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        error: str,
    ) -> str:
        from cognis.models.workflow import GateConfig, GateOption

        if (task.interaction_mode_override or workflow.interaction.mode) == "none":
            return "fail"
        gate = StepDefinition(
            name=f"{step_def.name}:error",
            type="gate",
            gate=GateConfig(
                message=(
                    f"Deterministic step '{step_def.name}' requires operator review.\n\n"
                    f"{' '.join(error.split())[:500]}"
                ),
                options=[
                    GateOption(label="Continue", action="continue"),
                    GateOption(label="Cancel task", action="cancel"),
                ],
            ),
        )
        result = await self._handle_gate_step(task, gate, state, workflow)
        return result if result in {"continue", "cancel", "fail"} else "paused"

    async def _handle_transient_executor_unavailable(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        exc: TransientExecutorUnavailable,
    ) -> bool:
        """Defer a task when its selected executor is temporarily unavailable.

        Returns True when the task was requeued for a later run. Returns False
        after the bounded deferral budget is exhausted; callers pause the task
        as infrastructure-blocked instead of failing the workflow.
        """

        attempt_key = f"transient_executor_unavailable:{step_def.name}"
        deferrals = state.loop_iterations.get(attempt_key, 0) + 1
        state.loop_iterations[attempt_key] = deferrals
        state.current_step_status = None
        state.last_retry_reason = None
        state.last_evaluation_feedback = None

        if deferrals > TRANSIENT_EXECUTOR_MAX_DEFERRALS:
            message = (
                "Workflow paused: selected executor is still unavailable after "
                f"{TRANSIENT_EXECUTOR_MAX_DEFERRALS} deferrals. Last error: {exc}"
            )
            logger.warning(
                "Pausing workflow because selected executor stayed unavailable",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                        "executor_id": exc.executor_id,
                        "deferrals": deferrals,
                    }
                },
            )
            state.status = "paused"
            state.current_step_status = "paused"
            state.pending_pause_payload = {
                "kind": "infrastructure_blocked",
                "message": message,
                "executor_id": exc.executor_id,
                "step_name": step_def.name,
                "deferrals": deferrals,
            }
            task.result_summary = message
            return False

        retry_after_seconds = max(1, int(exc.retry_after_seconds or 0))
        scheduled_for = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        task.status = TaskStatus.READY
        task.scheduled_for = scheduled_for
        task.result_summary = (
            "Workflow deferred: selected executor is not connected or not ready "
            f"(retry {deferrals}/{TRANSIENT_EXECUTOR_MAX_DEFERRALS})."
        )
        state.status = "running"
        state.pending_pause_type = None
        state.pending_pause_payload = None
        state.version += 1

        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            ok = await defer_running_task(
                db_session,
                task.task_id,
                scheduled_for=scheduled_for,
                workflow_state=state.model_dump(mode="json"),
                result_summary=task.result_summary,
            )
            if not ok:
                raise StepInterrupted(task.task_id)
            await db_session.commit()

        logger.info(
            "Deferred workflow while selected executor reconnects",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "executor_id": exc.executor_id,
                    "deferrals": deferrals,
                    "scheduled_for": scheduled_for.isoformat(),
                }
            },
        )
        return True

    async def _execute_run_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        *,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[StepOutput | None, str]:
        """Execute a single run step via the agent loop."""

        # Resolve the step agent for prompt/tools and the primary agent whose
        # executor secondary/system steps inherit. Runtime selection stays
        # separate so a system step can never accidentally run on controller
        # fallback or a system-owned executor.
        primary_agent, agent = await self._resolve_step_agents(task, step_def)
        if primary_agent is None or agent is None:
            logger.warning(
                "Could not resolve agent for step",
                extra={"extra_data": {"task_id": task.task_id, "step": step_def.name}},
            )
            return None, ""
        step_agent_profile_id = step_def.agent_profile_id or task.agent_profile_id
        resolved_step_agent_profile = resolve_agent_profile(
            agent,
            step_agent_profile_id,
            source="workflow_step" if step_def.agent_profile_id else "task",
        )
        step_agent_profile_id = resolved_step_agent_profile.profile_id

        # Determine if this is a retry (re-attempt of a previously-run step)
        attempt = state.loop_iterations.get(f"attempts:{step_def.name}", 1)
        is_retry = attempt > 1

        # Determine step index
        step_index = self._find_step_index(workflow, step_def.name) or 0
        effective_input = resolve_effective_input(step_def, step_index, workflow.steps)
        reuse_source_name = effective_input.reuse_session_from

        # Session handling: reuse only when the latest prior session is still
        # reusable (active/idle). Re-entering an already approved step must
        # create a fresh session instead of reopening a completed one.
        has_prior_run = False
        if not is_retry:
            has_prior_run = await self._has_prior_step_session(task, step_def)

        seeded_from_prior = False
        session_policy = _effective_task_session_policy(task, workflow)
        reuse_source_run = None
        reuse_recovery: dict[str, Any] | None = None
        if reuse_source_name and not (is_retry or has_prior_run):
            (
                conversation,
                session,
                reuse_source_run,
                reuse_recovery,
            ) = await self._reuse_source_step_session(
                task,
                step_def,
                agent,
                source_name=reuse_source_name,
                agent_profile_id=step_agent_profile_id,
                session_policy=session_policy,
            )
        elif is_retry or has_prior_run:
            conversation, session, seeded_from_prior = await self._reuse_or_create_step_session(
                task,
                step_def,
                agent,
                agent_profile_id=step_agent_profile_id,
                session_policy=session_policy,
                require_same_conversation=bool(reuse_source_name),
            )
        else:
            conversation, session = await self._create_step_session(
                task,
                step_def,
                agent,
                agent_profile_id=step_agent_profile_id,
                session_policy=session_policy,
            )

        # For type="full" input, fork the source step's events into the new
        # session so the conversation appears as natural history.  Skipped on
        # retry (the session already has events from the prior attempt).
        if (
            not reuse_source_name
            and not is_retry
            and not seeded_from_prior
            and effective_input.type == "full"
        ):
            await self._fork_source_events(
                source_name=effective_input.single_source(),
                target_session=session,
                state=state,
                copy_prefix=not (agent.is_system or agent.agent_type == "secondary"),
            )

        if reuse_source_name:
            if self._session_cache is None:
                raise RuntimeError(
                    f"Cannot establish an evidence boundary for reused step {step_def.name!r}"
                )
            await self._refresh_reused_session_authoritatively(session, step_def.name)
            reset_tool_state = getattr(self._session_cache, "reset_step_tool_state", None)
            if callable(reset_tool_state):
                await reset_tool_state(session.session_id)

        step_event_start_seq = self._session_last_event_seq(session.session_id) + 1
        persisted_todos: list[dict[str, Any]] = []
        latest_step_run = None
        previous_runtime_info: dict[str, Any] = {}
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            latest_step_run = await get_latest_step_run_for_task_step(
                db_session,
                task.task_id,
                step_def.name,
                attempt_number=task.attempt_number,
                current_revision_only=True,
            )
            raw_todos = latest_step_run.todos if latest_step_run is not None else None
            if latest_step_run is not None and isinstance(latest_step_run.runtime_info, dict):
                previous_runtime_info = dict(latest_step_run.runtime_info)
            if reuse_source_name and reuse_recovery is None and latest_step_run is not None:
                reuse_recovery = {
                    "reason": (
                        "reattached_target_session"
                        if latest_step_run.session_id == session.session_id
                        else "rotated_target_session"
                    ),
                    "source_session_id": latest_step_run.session_id,
                    "selected_session_id": session.session_id,
                }
            if isinstance(raw_todos, list):
                persisted_todos = [item for item in raw_todos if isinstance(item, dict)]
            if is_retry and state.last_retry_reason in {
                "evaluation_rejected",
                "routed_revision",
            }:
                persisted_todos = self._todos_for_evaluation_retry(state, persisted_todos)

            if latest_step_run is None:
                step_run_id = f"sr_{uuid.uuid4().hex}"
                await create_step_run(
                    db_session,
                    task_id=task.task_id,
                    step_name=step_def.name,
                    step_type=step_def.type,
                    agent_id=agent.agent_id,
                    agent_profile_id=step_agent_profile_id,
                    attempt=attempt,
                    attempt_number=task.attempt_number,
                    step_run_id=step_run_id,
                    conversation_id=conversation.conversation_id,
                    workspace_root=task.workspace_root,
                    working_directory=task.working_directory,
                    require_deliverable=step_def.require_deliverable,
                )
            else:
                step_run_id = latest_step_run.step_run_id
                previous_start_seq = previous_runtime_info.get("step_event_start_seq")
                if isinstance(previous_start_seq, int):
                    step_event_start_seq = previous_start_seq
            update_kwargs: dict[str, Any] = {
                "status": "running",
                "attempt": attempt,
                "conversation_id": conversation.conversation_id,
                "session_id": session.session_id,
                "intaris_session_id": session.intaris_session_id,
                "workspace_root": task.workspace_root,
                "working_directory": task.working_directory,
                "require_deliverable": step_def.require_deliverable,
                "agent_profile_id": step_agent_profile_id,
                "output": None,
                "evaluation": None,
                "todos": persisted_todos,
                "started_at": datetime.now(UTC),
                "completed_at": None,
                "runtime_info": {
                    **previous_runtime_info,
                    "step_event_start_seq": step_event_start_seq,
                    **(
                        {
                            "reuse_session_from": reuse_source_name,
                            "reuse_source_step_run_id": getattr(
                                reuse_source_run, "step_run_id", None
                            )
                            or previous_runtime_info.get("reuse_source_step_run_id"),
                            "reuse_recovery": reuse_recovery or {"reason": "target_retry"},
                        }
                        if reuse_source_name
                        else {}
                    ),
                },
            }
            if latest_step_run is not None:
                update_kwargs["deliverable_id"] = None
            await update_step_run(db_session, step_run_id, **update_kwargs)
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
        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is not None:
            await cluster_signals.publish_task_change(task.task_id, step_run_id=step_run_id)

        # Resolve tool registry and executor for this step.
        try:
            executor_agent = self._executor_agent_for_step(primary_agent, agent)
            interaction_mode = task.interaction_mode_override or workflow.interaction.mode
            access_context = RuntimeAccessContext(
                user_email=task.created_by,
                agent_id=executor_agent.agent_id,
                agent_owner_email=executor_agent.owner_email,
                agent_type=executor_agent.agent_type,
                session_id=session.session_id,
                conversation_id=getattr(conversation, "conversation_id", None)
                if conversation is not None
                else None,
                task_id=task.task_id,
                step_name=step_def.name,
                step_run_id=step_run_id,
                parent_session_id=session.parent_session_id,
                delegation_mode=session.delegation_mode,
                workflow_step=True,
                interaction_mode=interaction_mode,
                session_policy=session_policy,
            )
            runtime_workspace_root, runtime_working_directory = _resolve_task_execution_paths(task)
            with scoped_runtime_context(
                workspace_root=runtime_workspace_root,
                effective_working_directory=runtime_working_directory,
            ):
                runtime = await self._resolve_step_runtime(
                    agent=agent,
                    executor_agent=executor_agent,
                    user_email=task.created_by,
                    access_context=access_context,
                    conversation_id=getattr(conversation, "conversation_id", None)
                    if conversation is not None
                    else None,
                    task_id=task.task_id,
                )
            runtime_workspace_root, runtime_working_directory = _resolve_task_execution_paths(
                task,
                executor_home=getattr(runtime.executor_environment, "home", None),
                executor_cwd=getattr(runtime.executor_environment, "cwd", None),
            )
            task.workspace_root = runtime_workspace_root
            task.working_directory = runtime_working_directory
        except TransientExecutorUnavailable:
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    step_run_id,
                    status="pending",
                    output={
                        "summary": "Deferred because the selected executor is not ready.",
                    },
                    runtime_info={
                        **previous_runtime_info,
                        "step_event_start_seq": step_event_start_seq,
                        **(
                            {
                                "reuse_session_from": reuse_source_name,
                                "reuse_source_step_run_id": getattr(
                                    reuse_source_run, "step_run_id", None
                                )
                                or previous_runtime_info.get("reuse_source_step_run_id"),
                                "reuse_recovery": reuse_recovery or {"reason": "target_retry"},
                            }
                            if reuse_source_name
                            else {}
                        ),
                        "runtime_source": "unresolved",
                        "failure_reason": "transient_executor_unavailable",
                        "agent_id": agent.agent_id,
                    },
                    completed_at=None,
                )
                await db_session.commit()
            raise
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logger.warning(
                "workflow: failed to resolve step runtime",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step_name": step_def.name,
                        "agent_id": agent.agent_id,
                        "error_type": exc.__class__.__name__,
                    }
                },
                exc_info=True,
            )
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    step_run_id,
                    status="failed",
                    output={
                        "summary": "Failed to resolve step runtime.",
                        "error": error,
                    },
                    runtime_info={
                        **previous_runtime_info,
                        "step_event_start_seq": step_event_start_seq,
                        **(
                            {
                                "reuse_session_from": reuse_source_name,
                                "reuse_source_step_run_id": getattr(
                                    reuse_source_run, "step_run_id", None
                                )
                                or previous_runtime_info.get("reuse_source_step_run_id"),
                                "reuse_recovery": reuse_recovery or {"reason": "target_retry"},
                            }
                            if reuse_source_name
                            else {}
                        ),
                        "runtime_source": "unresolved",
                        "failure_reason": error,
                        "agent_id": agent.agent_id,
                    },
                    completed_at=datetime.now(UTC),
                )
                await db_session.commit()
            return None, step_run_id
        if runtime.runtime_info is not None:
            runtime_info = {
                **previous_runtime_info,
                **runtime.runtime_info,
                **resolved_step_agent_profile.audit_metadata(),
                "step_event_start_seq": step_event_start_seq,
            }
            if reuse_source_name:
                runtime_info.update(
                    {
                        "reuse_session_from": reuse_source_name,
                        "reuse_source_step_run_id": getattr(reuse_source_run, "step_run_id", None)
                        or previous_runtime_info.get("reuse_source_step_run_id"),
                        "reuse_recovery": reuse_recovery or {"reason": "target_retry"},
                    }
                )
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    step_run_id,
                    runtime_info=runtime_info,
                )
                await db_session.commit()

        # Log resolved step input for debugging.  Prior step context is
        # delivered through event forking (type="full") or the step prompt
        # (type="last"/"summary"), not through a separate prior_context.
        resolved_sources = resolve_source_names(step_def, step_index, workflow.steps)
        input_source_runs = (
            await self._current_input_source_runs(task, resolved_sources)
            if reuse_source_name
            else {}
        )
        input_source_step_run_ids = {
            name: row.step_run_id for name, row in input_source_runs.items()
        }
        input_source_session_ids = {
            name: row.session_id
            for name, row in input_source_runs.items()
            if isinstance(row.session_id, str)
        }
        previously_injected_source_step_run_ids: dict[str, str] = {}
        provenance_runtime_infos = [previous_runtime_info]
        if reuse_source_run is not None and isinstance(reuse_source_run.runtime_info, dict):
            provenance_runtime_infos.append(reuse_source_run.runtime_info)
        for provenance_runtime_info in provenance_runtime_infos:
            raw_provenance = provenance_runtime_info.get("reuse_injected_step_run_ids")
            if not isinstance(raw_provenance, dict):
                continue
            previously_injected_source_step_run_ids.update(
                {
                    str(name): str(step_run_id)
                    for name, step_run_id in raw_provenance.items()
                    if isinstance(name, str) and isinstance(step_run_id, str)
                }
            )
        if reuse_source_name and reuse_source_run is not None:
            previously_injected_source_step_run_ids[reuse_source_name] = (
                reuse_source_run.step_run_id
            )
        prompt_boundary_recorded = self._step_prompt_boundary_recorded(
            session.session_id,
            step_run_id,
        )
        if prompt_boundary_recorded:
            previously_injected_source_step_run_ids.update(input_source_step_run_ids)
        if reuse_source_name:
            merged_runtime_info = {
                **previous_runtime_info,
                **(runtime.runtime_info or {}),
                **resolved_step_agent_profile.audit_metadata(),
                "step_event_start_seq": step_event_start_seq,
                "reuse_session_from": reuse_source_name,
                "reuse_source_step_run_id": getattr(reuse_source_run, "step_run_id", None)
                or previous_runtime_info.get("reuse_source_step_run_id"),
                "reuse_recovery": reuse_recovery or {"reason": "target_retry"},
                "reuse_injected_step_run_ids": previously_injected_source_step_run_ids,
            }
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    step_run_id,
                    runtime_info=merged_runtime_info,
                )
                await db_session.commit()
        logger.info(
            "workflow: resolved step input",
            extra={
                "extra_data": {
                    "step": step_def.name,
                    "input_type": effective_input.type,
                    "sources": resolved_sources,
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
            step_orchestration = OrchestrationMode.TASK_PRIMARY

        project_context = await self._build_project_context(task)

        async def _consume_context_comments(reason: str) -> list[dict[str, Any]]:
            async with self._session_factory() as db_session:
                rows = await list_pending_context_task_comments(
                    db_session,
                    task_id=task.task_id,
                    step_name=step_def.name,
                    attempt_number=task.attempt_number,
                )

            return [
                {
                    "content": render_context_comment(
                        comment_id=row.comment_id,
                        author_email=row.author_email,
                        body=row.body,
                        target_step=row.target_step,
                    ),
                    "attachments": [],
                    "system_initiated": False,
                    "follow_up": None,
                    "source": "task_context_comment",
                    "author_email": row.author_email,
                    "comment_id": row.comment_id,
                    "applied_reason": reason,
                }
                for row in rows
            ]

        async def _ack_context_comment(item: dict[str, Any]) -> None:
            if item.get("source") != "task_context_comment":
                return
            comment_id = item.get("comment_id")
            if not isinstance(comment_id, str) or not comment_id:
                return
            async with self._session_factory() as db_session:
                await mark_context_task_comment_applied(
                    db_session,
                    comment_id=comment_id,
                    step_name=step_def.name,
                    step_run_id=step_run_id,
                    reason=str(item.get("applied_reason") or "workflow_boundary"),
                )
                await db_session.commit()

        # Build step context. Primary task steps can coordinate restricted,
        # task-owned workstreams; secondary reviewer steps cannot orchestrate.
        ctx = StepContext(
            step_definition=step_def,
            session=session,
            conversation=conversation,
            agent=agent,
            executor_agent=executor_agent,
            task_id=task.task_id,
            task_title=task.title,
            task_description=task.description,
            task_expected_output=task.expected_output,
            task_source_type=task.source_type,
            task_source_ref=task.source_ref,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.name,
            project_context=project_context,
            completion_delivery=task.completion_delivery,
            workspace_root=runtime_workspace_root,
            working_directory=runtime_working_directory,
            workspace_root_explicit=bool(task.workspace_root),
            working_directory_explicit=bool(task.working_directory),
            step_run_id=step_run_id,
            policy=step_policy,
            is_retry=is_retry,
            user_message_already_recorded=prompt_boundary_recorded,
            user_message=task.description or task.title,
            prior_context=prior_context,
            interaction_mode=interaction_mode,
            session_policy=session_policy,
            tool_registry=runtime.tool_registry,
            executor_connection=runtime.executor_connection,
            executor_environment=runtime.executor_environment,
            executor_pool=getattr(runtime, "executor_pool", None),
            active_executor_id=getattr(runtime, "active_executor_id", None),
            runtime_info={
                **(runtime.runtime_info or {}),
                **resolved_step_agent_profile.audit_metadata(),
            },
            workflow_state=state,
            workflow_steps=workflow.steps,
            step_index=step_index,
            session_reuse_source_step=reuse_source_name,
            input_source_step_run_ids=input_source_step_run_ids,
            input_source_session_ids=input_source_session_ids,
            previously_injected_source_step_run_ids=previously_injected_source_step_run_ids,
            cancel_event=cancel_event,
            orchestration_mode=step_orchestration,
            turn_id=step_run_id,
            todos=persisted_todos,
            execution_fence=current_task_execution_fence(),
            consume_boundary_batch=(
                _consume_context_comments if agent.agent_type != "secondary" else None
            ),
            on_boundary_persisted=(
                _ack_context_comment if agent.agent_type != "secondary" else None
            ),
        )

        async def on_thinking(
            block_id: str,
            delta: str,
            title: str | None,
            complete: bool,
            content: str | None = None,
            started_at: str | None = None,
            completed_at: str | None = None,
            duration_ms: int | None = None,
            source: str | None = None,
            provider_block_index: int | None = None,
        ) -> None:
            if hasattr(self._session_cache, "update_active_thinking"):
                self._session_cache.update_active_thinking(
                    session.session_id,
                    message_id=step_run_id,
                    turn_id=step_run_id,
                    block_id=block_id,
                    delta=delta,
                    title=title,
                    complete=complete,
                    content=content,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    source=source,
                    provider_block_index=provider_block_index,
                )

        async def on_tool_progress(
            call_id: str,
            tool_name: str,
            progress: dict[str, Any],
        ) -> None:
            if on_progress is not None:
                phase = str(progress.get("phase") or "preparing_input")
                input_lines = progress.get("input_lines")
                input_chars = progress.get("input_chars")
                if tool_name == "apply_patch" and phase == "preparing_input":
                    details: list[str] = []
                    if isinstance(input_lines, int) and input_lines > 0:
                        details.append(f"{input_lines:,} lines")
                    if isinstance(input_chars, int) and input_chars > 0:
                        details.append(f"{input_chars:,} chars")
                    suffix = f" ({', '.join(details)})" if details else ""
                    await on_progress(f"\n\nPreparing apply_patch input{suffix}…")

        # Run agent loop
        try:
            with scoped_runtime_context(
                user_email=session.user_email,
                agent_id=executor_agent.agent_id,
                agent_owner_email=executor_agent.owner_email,
                workspace_root=ctx.workspace_root,
                effective_working_directory=ctx.working_directory,
                executor_environment=runtime.executor_environment,
                access_context=access_context,
            ):
                await self._session_manager.refresh_intaris_session_policy(session)
                output = await self._agent_loop.run_step(
                    ctx,
                    on_token=on_progress,
                    on_thinking=on_thinking,
                    on_tool_progress=on_tool_progress,
                )
        except StepInterrupted:
            current_status = await self._read_task_status(task.task_id)
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    step_run_id,
                    status="cancelled" if current_status == TaskStatus.CANCELLED else "paused",
                    completed_at=datetime.now(UTC),
                )
                await db_session.commit()
            raise
        except StaleTaskExecutionOwner:
            raise
        except Exception:
            async with self._session_factory() as db_session:
                await assert_task_execution_fence(db_session)
                await update_step_run(
                    db_session,
                    step_run_id,
                    status="failed",
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
        completion = self._resolve_completion(step_def, workflow)
        missing_required_deliverable = self._missing_required_deliverable(step_def, output)
        if missing_required_deliverable and output is not None:
            output.error = (
                f"Step {step_def.name!r} requires a deliverable, but the step finished without one."
            )
        step_failed = output is None or output.error is not None or missing_required_deliverable
        if step_failed:
            initial_status = "failed"
        else:
            assert output is not None
            if output.outcome is not None and output.outcome.status == "failed":
                initial_status = "evaluating" if (completion and completion.evaluate) else "failed"
            else:
                initial_status = (
                    "evaluating" if (completion and completion.evaluate) else "approved"
                )
        step_event_end_seq = self._session_last_event_seq(session.session_id)
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
            latest = await get_latest_step_run_for_task_step(
                db_session,
                task.task_id,
                step_def.name,
                attempt_number=task.attempt_number,
                current_revision_only=True,
            )
            final_runtime_info = (
                dict(latest.runtime_info)
                if latest is not None and isinstance(latest.runtime_info, dict)
                else {}
            )
            final_runtime_info["step_event_start_seq"] = step_event_start_seq
            final_runtime_info["step_event_end_seq"] = step_event_end_seq
            if reuse_source_name:
                final_runtime_info["reuse_injected_step_run_ids"] = {
                    **previously_injected_source_step_run_ids,
                    **input_source_step_run_ids,
                }
            await update_step_run(
                db_session,
                step_run_id,
                status=initial_status,
                deliverable_id=(
                    output.deliverable_id if output and output.deliverable_id else None
                ),
                output=output.model_dump(mode="json") if output else None,
                runtime_info=final_runtime_info,
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
        return (None if step_failed else output), step_run_id

    async def _evaluate_step(
        self,
        step_def: StepDefinition,
        step_output: StepOutput,
        state: WorkflowState,
        task: TaskModel,
        workflow: Workflow,
    ) -> StepEvaluation:
        """Run the step evaluator."""
        step_output = await self._canonicalize_step_output_for_evaluation(step_output)
        step_index = self._find_step_index(workflow, step_def.name) or 0
        source_names = resolve_source_names(step_def, step_index, workflow.steps)

        step_inputs: dict[str, StepOutput] = {}
        for source_name in source_names:
            raw = state.step_outputs.get(source_name)
            if raw:
                step_inputs[source_name] = StepOutput.model_validate(raw)

        step_event_start_seq: int | None = None
        step_event_end_seq: int | None = None
        async with self._session_factory() as db_session:
            current_run = await get_latest_step_run_for_task_step(
                db_session,
                task.task_id,
                step_def.name,
                attempt_number=task.attempt_number,
                current_revision_only=True,
            )
        if current_run is not None and isinstance(current_run.runtime_info, dict):
            raw_start = current_run.runtime_info.get("step_event_start_seq")
            raw_end = current_run.runtime_info.get("step_event_end_seq")
            if isinstance(raw_start, int):
                step_event_start_seq = raw_start
            if isinstance(raw_end, int):
                step_event_end_seq = raw_end
        execution_evidence = await self._build_step_execution_evidence(
            step_output,
            event_start_seq=step_event_start_seq,
            event_end_seq=step_event_end_seq,
        )

        return await self._step_evaluator.evaluate(
            step_definition=step_def,
            step_output=step_output,
            step_inputs=step_inputs,
            task_context=self._build_step_task_context(task, state),
            execution_evidence=execution_evidence,
        )

    async def _canonicalize_step_output_for_evaluation(
        self,
        step_output: StepOutput,
    ) -> StepOutput:
        """Use persisted deliverable content as the evaluator source of truth."""

        if not step_output.deliverable_id:
            return step_output

        try:
            async with self._session_factory() as db_session:
                deliverable = await get_deliverable(db_session, step_output.deliverable_id)
        except Exception:
            logger.warning(
                "workflow: failed to load persisted deliverable for evaluation",
                extra={"extra_data": {"deliverable_id": step_output.deliverable_id}},
                exc_info=True,
            )
            return step_output

        if deliverable is None:
            logger.warning(
                "workflow: step output references missing deliverable during evaluation",
                extra={"extra_data": {"deliverable_id": step_output.deliverable_id}},
            )
            return step_output

        artifact_store = getattr(self._agent_loop, "artifact_store", None)
        if artifact_store is not None:
            try:
                await hydrate_deliverable_payload(deliverable, artifact_store)
            except Exception:
                logger.warning(
                    "workflow: failed to hydrate persisted deliverable for evaluation",
                    extra={"extra_data": {"deliverable_id": step_output.deliverable_id}},
                    exc_info=True,
                )
                return step_output

        persisted_content = getattr(deliverable, "content", None)
        if not isinstance(persisted_content, str) or not persisted_content.strip():
            logger.warning(
                "workflow: persisted deliverable has invalid content during evaluation",
                extra={
                    "extra_data": {
                        "deliverable_id": step_output.deliverable_id,
                        "content_type": type(persisted_content).__name__,
                    }
                },
            )
            return step_output

        metadata = dict(step_output.metadata or {})
        metadata["evaluator_deliverable_source"] = {
            "source": "persisted_deliverable",
            "deliverable_id": step_output.deliverable_id,
            "content_mirror_changed": persisted_content != step_output.content,
        }
        if persisted_content != step_output.content:
            logger.warning(
                "workflow: evaluator deliverable content mirror mismatch; using persisted content",
                extra={
                    "extra_data": {
                        "deliverable_id": step_output.deliverable_id,
                        "step_output_content_len": len(step_output.content or ""),
                        "persisted_content_len": len(persisted_content),
                    }
                },
            )

        return step_output.model_copy(
            update={
                "content": persisted_content,
                "metadata": metadata,
                "deliverable_version": getattr(
                    deliverable, "version", step_output.deliverable_version
                ),
                "deliverable_format": getattr(
                    deliverable, "format", step_output.deliverable_format
                ),
                "deliverable_title": getattr(deliverable, "title", step_output.deliverable_title),
            }
        )

    async def _build_step_execution_evidence(
        self,
        step_output: StepOutput,
        *,
        event_start_seq: int | None = None,
        event_end_seq: int | None = None,
    ) -> dict[str, Any]:
        """Combine step-reported evidence with actual session tool events."""

        base = (
            dict(step_output.execution_evidence)
            if isinstance(step_output.execution_evidence, dict)
            else {"tools": [], "files_read": [], "files_written": [], "commands": []}
        )
        session_id = step_output.intaris_session_id or step_output.session_id
        guardrails = getattr(self._providers, "guardrails", None)
        if not session_id or guardrails is None or not hasattr(guardrails, "read_events"):
            return base

        try:
            event_read = await guardrails.read_events(
                session_id=session_id,
                after_seq=max(0, event_start_seq - 1) if event_start_seq is not None else 0,
                types=["tool_call", "tool_result"],
                allow_missing_stream=True,
            )
        except TypeError:
            event_read = await guardrails.read_events(session_id=session_id, after_seq=0)
        except Exception:
            logger.warning(
                "workflow: failed to collect evaluator session evidence",
                extra={"extra_data": {"session_id": session_id}},
                exc_info=True,
            )
            base["session_events"] = {
                "session_id": session_id,
                "available": False,
                "error": "failed_to_read_events",
            }
            return base

        events = list(getattr(event_read, "events", []) or [])
        if event_start_seq is not None:
            events = [
                event
                for event in events
                if isinstance(event, dict)
                and isinstance(event.get("seq"), int)
                and event["seq"] >= event_start_seq
                and (event_end_seq is None or event["seq"] <= event_end_seq)
            ]
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for raw_event in events[-100:]:
            if not isinstance(raw_event, dict):
                continue
            event_type = str(raw_event.get("type") or "")
            data = raw_event.get("data") if isinstance(raw_event.get("data"), dict) else {}
            seq = raw_event.get("seq")
            if event_type == "tool_call":
                arguments = data.get("arguments")
                tool_calls.append(
                    {
                        "seq": seq,
                        "call_id": data.get("call_id"),
                        "name": data.get("name") or data.get("tool_name"),
                        "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
                    }
                )
            elif event_type == "tool_result":
                result_text = (
                    data.get("content")
                    or data.get("result")
                    or data.get("output")
                    or data.get("summary")
                )
                tool_results.append(
                    {
                        "seq": seq,
                        "call_id": data.get("call_id"),
                        "name": data.get("name") or data.get("tool_name"),
                        "is_error": bool(data.get("is_error") or data.get("error")),
                        "summary": _short_evidence_text(result_text),
                    }
                )

        base["session_events"] = {
            "session_id": session_id,
            "available": True,
            "tool_call_count": len(tool_calls),
            "tool_result_count": len(tool_results),
            "tool_calls": tool_calls[-30:],
            "tool_results": tool_results[-30:],
            "event_start_seq": event_start_seq,
            "event_end_seq": event_end_seq,
        }
        return base

    async def _handle_gate_step(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
    ) -> str:
        """Handle a gate step — pause and wait for caller response."""
        if (task.interaction_mode_override or workflow.interaction.mode) == "none":
            # Autonomous mode — gates become continue
            GATES_TOTAL.labels(action="auto_continue").inc()
            return "continue"

        gate = step_def.gate
        if gate is None:
            return "continue"

        async def _persist_gate_evaluation(details: dict[str, Any]) -> None:
            async with self._session_factory() as db_session:
                latest = await get_latest_step_run_for_task_step(
                    db_session,
                    task.task_id,
                    step_def.name,
                    attempt_number=task.attempt_number,
                    current_revision_only=True,
                )
                if latest is None:
                    step_run_id = f"sr_{uuid.uuid4().hex}"
                    initial_status = (
                        "approved" if details.get("action_taken") == "continue" else "paused"
                    )
                    gate_recorded_at = datetime.now(UTC)
                    await create_step_run(
                        db_session,
                        task_id=task.task_id,
                        step_name=step_def.name,
                        step_type=step_def.type,
                        agent_id=task.agent_id,
                        attempt=1,
                        attempt_number=task.attempt_number,
                        step_run_id=step_run_id,
                        status=initial_status,
                        workspace_root=task.workspace_root,
                        working_directory=task.working_directory,
                        started_at=gate_recorded_at,
                        completed_at=gate_recorded_at,
                        runtime_info={"gate_evaluation": details},
                    )
                else:
                    step_run_id = latest.step_run_id
                    runtime_info = dict(latest.runtime_info or {})
                    runtime_info["gate_evaluation"] = details
                    gate_recorded_at = datetime.now(UTC)
                    await update_step_run(
                        db_session,
                        step_run_id,
                        status=(
                            "approved" if details.get("action_taken") == "continue" else "paused"
                        ),
                        runtime_info=runtime_info,
                        started_at=latest.started_at or gate_recorded_at,
                        completed_at=gate_recorded_at,
                    )
                await db_session.commit()

        if gate.conditions:
            gate_evaluation = evaluate_gate_conditions_detailed(
                [condition.expression for condition in gate.conditions],
                step_outputs=state.step_outputs,
                thresholds=gate.thresholds,
            )
            if gate_evaluation["errors"]:
                logger.warning(
                    "gate condition evaluation failed; requiring conditional gate",
                    extra={"extra_data": {"task_id": task.task_id, "step_name": step_def.name}},
                )
                should_pause = True
                gate_evaluation.update(
                    {"branch": "error_pause", "action_taken": "pause", "passed": True}
                )
            else:
                should_pause = bool(gate_evaluation["passed"])
                gate_evaluation.update(
                    {
                        "branch": "condition_pass" if should_pause else "condition_skip",
                        "action_taken": "pause" if should_pause else "continue",
                    }
                )
            if not should_pause:
                await _persist_gate_evaluation(gate_evaluation)
                GATES_TOTAL.labels(action="condition_skip").inc()
                return "continue"
        else:
            gate_evaluation = {
                "condition_mode": "any",
                "conditions": [],
                "passed": True,
                "errors": [],
                "branch": "unconditional_pause",
                "action_taken": "pause",
            }

        # Build gate context from specified inputs
        gate_context: dict[str, Any] = {}
        for input_name in gate.input:
            raw = state.step_outputs.get(input_name)
            if raw:
                gate_context[input_name] = raw

        existing_pause_payload = state.pending_pause_payload or {}
        reuse_pause = (
            state.pending_pause_type == "gate"
            and state.status == "paused"
            and str(existing_pause_payload.get("step_name") or "") == step_def.name
        )
        pause_id = str(existing_pause_payload.get("pause_id") or "").strip() if reuse_pause else ""
        if not pause_id:
            pause_id = f"gate_{uuid.uuid4().hex[:12]}"

        existing_notification = None
        if reuse_pause and self._notification_service is not None:
            existing_notification = await self._notification_service.get(pause_id)
            if existing_notification is None:
                reuse_pause = False
            elif existing_notification.status == "resolved":
                resolution = existing_notification.resolution or {}
                action = str(resolution.get("decision") or "continue")
                instruction = str(
                    resolution.get("note") or resolution.get("feedback") or ""
                ).strip()
                GATES_TOTAL.labels(action=action).inc()
                gate_evaluation.update({"action_taken": action, "branch": "operator_decision"})
                await _persist_gate_evaluation(gate_evaluation)
                state.status = "running"
                state.current_step_status = "running"
                state.pending_pause_type = None
                state.pending_pause_payload = None
                if instruction and action != "cancel":
                    state.last_operator_instruction = instruction
                task.status = TaskStatus.RUNNING
                await self._persist_workflow_state(task, sync_status=True)
                return action

        # Pause workflow — write status + workflow_state atomically
        state.status = "paused"
        state.current_step_status = "paused"
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

        if not reuse_pause:
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
        elif self._pause_waiter.get(pause_id) is None:
            self._pause_waiter.register(
                PendingPause(
                    pause_id=pause_id,
                    pause_type="gate",
                    task_id=task.task_id,
                    step_name=step_def.name,
                    conversation_id=task.source_ref or "",
                    question=gate.message,
                    options=gate_options,
                    context=gate_context,
                )
            )

        # Trigger a follow-up turn in the source conversation so the agent
        # can explain the pause to the user (why it paused, what the options are).
        if not reuse_pause and task.source_type == "chat" and task.source_ref:
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
                            session_id=task.source_session_id,
                            source_type="task_gate_follow_up",
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

            follow_up = self._follow_up_policy.build_gate_follow_up(
                conversation_id=task.source_ref,
                pause_id=pause_id,
                task_id=task.task_id,
                task_title=task.title,
                gate_message=gate.message,
                gate_options=gate_options,
            )

            await self._event_bus.publish(
                Event(
                    type=EventType.FOLLOW_UP_TURN_REQUESTED,
                    data={
                        "conversation_id": task.source_ref,
                        "origin_session_id": task.source_session_id,
                        "follow_up": follow_up.model_dump(mode="json"),
                        "delivery_id": delivery_id,
                        "channel_deliverable": channel_deliverable,
                        "delivery_fallback_text": delivery_fallback_text,
                    },
                )
            )

        # Wait for resolution
        timeout_seconds = float(max(1, gate.timeout_seconds))
        try:
            wait_for_resolution = getattr(self._notification_service, "wait_for_resolution", None)
            if callable(wait_for_resolution):
                resolution = await wait_for_resolution(
                    pause_id,
                    timeout=timeout_seconds,
                )
            else:
                resolution = await self._pause_waiter.wait(pause_id, timeout=timeout_seconds)
            action = resolution.decision
            instruction = str(
                resolution.data.get("note") or resolution.data.get("feedback") or ""
            ).strip()
        except TimeoutError:
            logger.warning(
                "Gate timed out",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "step": step_def.name,
                        "pause_id": pause_id,
                        "timeout_seconds": timeout_seconds,
                        "timeout_action": gate.timeout_action,
                    }
                },
            )
            if self._notification_service is not None:
                await self._notification_service.resolve(
                    pause_id,
                    gate.timeout_action,
                    {"reason": "timeout"},
                    user_email=task.created_by,
                )
            action = gate.timeout_action
            instruction = ""

        GATES_TOTAL.labels(action=action).inc()
        gate_evaluation.update({"action_taken": action, "branch": "operator_decision"})
        await _persist_gate_evaluation(gate_evaluation)

        # Resume — write status + workflow_state atomically
        state.status = "running"
        state.current_step_status = "running"
        revise_target = _parse_revise_action(action)
        if revise_target is not None and not state.last_revision_context:
            revision_parts = [
                f"Gate '{step_def.name}' requested revision of step '{revise_target}'."
            ]
            if gate.message:
                revision_parts.append(f"Gate message: {gate.message}")
            if gate_context:
                revision_parts.append(f"Gate context:\n{gate_context}")
            if instruction:
                revision_parts.append(f"Operator note:\n{instruction}")
            state.last_revision_context = "\n\n".join(revision_parts)
        state.pending_pause_type = None
        state.pending_pause_payload = None
        if instruction and action != "cancel":
            state.last_operator_instruction = instruction
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

        state.last_retry_reason = reason
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

    async def _handle_step_outcome(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        step_result: StepOutput,
        state: WorkflowState,
        workflow: Workflow,
    ) -> str:
        """Apply post-approval routing based on the completed step outcome."""

        outcome = step_result.outcome
        outcome_status = outcome.status if outcome is not None else "success"
        route = self._resolve_outcome_route(step_def, outcome_status)
        if route is None:
            state.last_evaluation_feedback = None
            state.last_retry_reason = None
            state.last_revision_context = None
            state.last_operator_instruction = None
            return "continue"
        action = route.action

        logger.info(
            "Applying step outcome route",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "outcome_status": outcome_status,
                    "action": action,
                }
            },
        )

        if action == "continue":
            state.last_evaluation_feedback = None
            state.last_retry_reason = None
            state.last_revision_context = None
            state.last_operator_instruction = None
            return "continue"
        if action == "fail":
            return "failed"
        if action == "cancel":
            return "cancelled"
        if action == "gate":
            if (task.interaction_mode_override or workflow.interaction.mode) == "none":
                logger.warning(
                    "Outcome gate requested in autonomous mode, failing workflow",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "step": step_def.name,
                            "outcome_status": outcome_status,
                        }
                    },
                )
                return "failed"

            gate_result = await self._handle_gate_step(
                task,
                StepDefinition(
                    name=f"{step_def.name}_outcome_gate",
                    type="gate",
                    gate=_build_outcome_gate(
                        step_def, outcome_reason=outcome.reason if outcome else None
                    ),
                ),
                state,
                workflow,
            )
            if gate_result == "continue":
                state.last_evaluation_feedback = None
                state.last_retry_reason = None
                state.last_revision_context = None
                state.current_step_index += 1
                await self._persist_workflow_state(task)
                return "routed"
            if gate_result == "cancel":
                return "cancelled"
            if gate_result == "fail":
                return "failed"
            revise_target = _parse_revise_action(gate_result)
            if revise_target is None:
                logger.error(
                    "Outcome gate returned unsupported action",
                    extra={"extra_data": {"task_id": task.task_id, "action": gate_result}},
                )
                return "failed"
            target_idx = self._find_step_index(workflow, revise_target)
            if target_idx is None:
                logger.error(
                    "Outcome gate revise target step not found",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "revise_target": revise_target,
                        }
                    },
                )
                return "failed"
            state.current_step_index = target_idx
            state.loop_iterations.pop(f"attempts:{revise_target}", None)
            state.last_evaluation_feedback = outcome.reason if outcome else None
            state.last_revision_context = self._build_revision_context(step_def, step_result)
            await self._persist_workflow_state(task)
            return "routed"

        revise_target = _parse_revise_action(action)
        if revise_target is not None:
            loop_result = await self._handle_outcome_review_loop(
                task,
                step_def,
                state,
                workflow,
                route.max_loop_iterations,
                route.on_exhausted,
                action,
                outcome.reason if outcome else None,
            )
            if loop_result is not None:
                return loop_result
            target_idx = self._find_step_index(workflow, revise_target)
            if target_idx is None:
                logger.error(
                    "Outcome route target step not found",
                    extra={
                        "extra_data": {
                            "task_id": task.task_id,
                            "step": step_def.name,
                            "revise_target": revise_target,
                        }
                    },
                )
                return "failed"
            state.current_step_index = target_idx
            state.loop_iterations.pop(f"attempts:{revise_target}", None)
            state.last_evaluation_feedback = outcome.reason if outcome else None
            state.last_revision_context = self._build_revision_context(step_def, step_result)
            await self._persist_workflow_state(task)
            return "routed"

        logger.error(
            "Unsupported outcome route action",
            extra={
                "extra_data": {"task_id": task.task_id, "step": step_def.name, "action": action}
            },
        )
        return "failed"

    async def _handle_outcome_review_loop(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        state: WorkflowState,
        workflow: Workflow,
        max_iterations: int | None,
        on_exhausted: str,
        action: str,
        feedback: str | None,
    ) -> str | None:
        """Apply loop limits for outcome routes that jump back to earlier steps."""

        if max_iterations is None:
            return None

        loop_key = f"outcome:{step_def.name}:{action}"
        current_iterations = state.loop_iterations.get(loop_key, 0)
        if current_iterations >= max_iterations:
            handled = await self._handle_exhausted(
                task,
                step_def,
                state,
                workflow,
                on_exhausted,
                last_error=feedback,
            )
            if not handled:
                return "failed"
            return "routed"

        state.loop_iterations[loop_key] = current_iterations + 1
        return None

    def _resolve_outcome_route(self, step_def: StepDefinition, outcome_status: str) -> Any | None:
        """Resolve the configured route for a completed step outcome."""

        for route in step_def.outcome_routes:
            if route.status == outcome_status:
                return route
        if outcome_status == "success":
            return None
        return SimpleNamespace(action="fail", max_loop_iterations=None, on_exhausted="fail")

    def _build_revision_context(self, step_def: StepDefinition, step_result: StepOutput) -> str:
        """Build revision context from the rejecting step's final assistant output."""

        parts = [
            f"The previous step `{step_def.name}` completed successfully but requested revisions."
        ]
        if step_result.outcome is not None:
            parts.append(f"\n\nOutcome: {step_result.outcome.status}")
            if step_result.outcome.reason:
                parts.append(f"\nReason: {step_result.outcome.reason}")
        if step_result.summary:
            parts.append(f"\n\nSummary:\n{step_result.summary}")
        if step_result.content:
            parts.append(f"\n\nReviewer Output:\n{step_result.content}")
        elif step_result.claims:
            claims_text = "\n".join(f"- {claim}" for claim in step_result.claims)
            parts.append(f"\n\nReviewer Claims:\n{claims_text}")
        return "".join(parts)

    def _build_step_task_context(self, task: TaskModel, state: WorkflowState) -> str:
        """Build evaluator task context with any one-shot operator instruction."""

        parts: list[str] = []
        if task.title:
            parts.append(f"Task title: {task.title}")
        if task.description:
            parts.append(f"Task description: {task.description}")
        if task.expected_output:
            parts.append(f"Expected output: {task.expected_output}")
        if state.last_operator_instruction:
            parts.append(
                f"Operator instruction for this step: {state.last_operator_instruction.strip()}"
            )
        return "\n\n".join(part for part in parts if part).strip()

    async def _build_project_context(self, task: TaskModel) -> str | None:
        """Build project metadata injected into workflow step prompts."""

        if not task.project_id:
            return None
        async with self._session_factory() as db_session:
            project = await get_project(db_session, task.project_id)
            if project is None:
                return None
            sources = await list_project_sources(db_session, task.project_id)
            workflow_ids = await list_project_workflow_ids(db_session, task.project_id)
        return build_project_context_message(project, sources, workflow_ids)

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
                    db_session,
                    task.task_id,
                    step_def.name,
                    attempt_number=task.attempt_number,
                    current_revision_only=True,
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
                events=with_session_events_turn_id([event], None),
                source="cognis",
                # A lost response after a successful append must not duplicate
                # the feedback event when the internal retry re-sends it.
                idempotency_key=(
                    f"{prior_run.intaris_session_id}:evaluation_feedback:{step_def.name}:{attempt}"
                ),
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
        interaction_mode = task.interaction_mode_override or workflow.interaction.mode
        logger.info(
            "Handling exhausted step",
            extra={
                "extra_data": {
                    "task_id": task.task_id,
                    "step": step_def.name,
                    "action": action,
                    "interaction_mode": interaction_mode,
                }
            },
        )

        if action == "continue":
            async with self._session_factory() as db_session:
                prior_run = await get_latest_step_run_for_task_step(
                    db_session,
                    task.task_id,
                    step_def.name,
                    attempt_number=task.attempt_number,
                    current_revision_only=True,
                )
                if prior_run is not None:
                    rejected_deliverable = await get_latest_rejected_deliverable_for_step_run(
                        db_session, prior_run.step_run_id
                    )
                    if rejected_deliverable is not None:
                        await update_deliverable_status(
                            db_session,
                            rejected_deliverable.deliverable_id,
                            status=DeliverableStatus.APPROVED,
                            evaluator_feedback=rejected_deliverable.evaluator_feedback,
                        )
                        await update_step_run(
                            db_session,
                            prior_run.step_run_id,
                            deliverable_id=rejected_deliverable.deliverable_id,
                        )
                        await db_session.commit()
                        artifact_store = getattr(self._agent_loop, "artifact_store", None)
                        if artifact_store is not None:
                            await hydrate_deliverable_payload(rejected_deliverable, artifact_store)
                        latest_output = (
                            dict(prior_run.output)
                            if isinstance(getattr(prior_run, "output", None), dict)
                            else {}
                        )
                        latest_output["deliverable_id"] = rejected_deliverable.deliverable_id
                        latest_output["deliverable_version"] = rejected_deliverable.version
                        latest_output["deliverable_format"] = rejected_deliverable.format
                        latest_output["deliverable_title"] = rejected_deliverable.title
                        latest_output["content"] = rejected_deliverable.content
                        latest_output.setdefault(
                            "summary",
                            rejected_deliverable.title or rejected_deliverable.content[:200],
                        )
                        state.step_outputs[step_def.name] = latest_output

            # Skip and advance — record the skipped step so the final
            # task status reflects that not all steps succeeded.
            state.skipped_steps.append(step_def.name)
            state.last_operator_instruction = None
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
            if interaction_mode == "none":
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
                state.last_evaluation_feedback = None
                state.last_retry_reason = None
                state.last_revision_context = None
                state.current_step_index += 1
                await self._persist_workflow_state(task)
                return True
            elif result == "cancel":
                state.status = "cancelled"
                state.last_operator_instruction = None
                state.current_step_index = len(workflow.steps)
                await self._persist_workflow_state(task)
                return True
            elif result == "fail":
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
        async with self._session_factory() as db_session:
            await assert_task_execution_fence(db_session)
        applied_mode = task.applied_completion_mode or "default"
        if applied_mode == "silent":
            logger.info(
                "task_delivery: explicit silent completion, skipping outward delivery",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            await self._publish_task_terminal_event(
                task,
                silent_delivery=True,
                delivery_skipped_reason="explicit_silent_completion",
            )
            return

        delivery_mode = task.delivery.mode
        target_conversation_id = await self._resolve_task_delivery_conversation(task)

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
            await self._publish_task_terminal_event(
                task,
                delivery_skipped_reason="no_target_conversation",
            )
            return

        async with self._session_factory() as db_session:
            target_conversation = await get_conversation(db_session, target_conversation_id)
            from cognis.store.queries import get_task, origin_session_is_in_active_scope

            conversation_origin = (
                task.source_type in {"chat", "agent"} and task.source_ref is not None
            )
            persisted_task = (
                await get_task(db_session, task.task_id) if conversation_origin else None
            )
            if persisted_task is not None and persisted_task.source_type not in {"chat", "agent"}:
                persisted_task = None
            origin_session_id = (
                persisted_task.source_session_id
                if persisted_task is not None
                else task.source_session_id
            )
            origin_is_current = not conversation_origin or persisted_task is None
            if origin_session_id is not None:
                scope_conversation_id = (
                    task.source_ref
                    if conversation_origin and task.source_ref is not None
                    else target_conversation_id
                )
                origin_is_current = await origin_session_is_in_active_scope(
                    db_session,
                    conversation_id=scope_conversation_id,
                    origin_session_id=origin_session_id,
                )
        if not origin_is_current:
            logger.info(
                "task_delivery: stale activity scope, retaining historical result",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "target_conversation_id": target_conversation_id,
                    }
                },
            )
            return

        if (
            applied_mode == "direct"
            and target_conversation
            and target_conversation.context_type != "web"
        ):
            await self._deliver_task_result_direct(task, target_conversation_id)
            return
        if applied_mode == "direct":
            logger.info(
                "task_delivery: using default web follow-up for direct completion",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "target_conversation_id": target_conversation_id,
                    }
                },
            )

        if delivery_mode == "silent":
            logger.info(
                "task_delivery: legacy silent delivery mode, skipping",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            await self._publish_task_terminal_event(
                task,
                conversation_id=target_conversation_id,
                silent_delivery=True,
                delivery_skipped_reason="legacy_silent_delivery",
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

        await self._deliver_task_result_default(task, target_conversation_id)

    async def _publish_task_terminal_event(self, task: TaskModel, **extra_data: object) -> None:
        """Publish the internal terminal task lifecycle event.

        Result delivery and task lifecycle propagation are separate concerns:
        silent or unresolved outward delivery must not suppress internal
        observers such as the scheduler.
        """

        event_type = EventType.TASK_FAILED
        if task.status == TaskStatus.COMPLETED:
            event_type = EventType.TASK_COMPLETED
        elif task.status == TaskStatus.CANCELLED:
            event_type = EventType.TASK_CANCELLED

        result_data = task.result_data if isinstance(task.result_data, dict) else {}
        data = {
            "task_id": task.task_id,
            "task_title": task.title,
            "title": task.title,
            "result_summary": task.result_summary,
            "attachments": result_data.get("attachments", []),
        }
        data.update(extra_data)

        await self._event_bus.publish(Event(type=event_type, data=data))
        cluster_signals = getattr(self, "cluster_signals", None)
        if cluster_signals is not None:
            await cluster_signals.publish_task_change(task.task_id)

    async def _resolve_task_delivery_conversation(self, task: TaskModel) -> str | None:
        """Resolve the conversation that should receive a task result."""

        delivery_mode = task.delivery.mode
        if delivery_mode == "same_conversation":
            return task.source_ref if task.source_type in {"chat", "agent"} else None
        if delivery_mode == "specific_conversation":
            return task.delivery.target
        if delivery_mode == "latest_active_for_agent":
            async with self._session_factory() as db_session:
                latest = await get_latest_active_conversation_for_agent(
                    db_session, task.created_by, task.agent_id
                )
            return latest.conversation_id if latest is not None else task.source_ref
        if delivery_mode == "preferred_channel":
            return await self._resolve_preferred_channel_conversation(task)
        if delivery_mode == "silent":
            return task.source_ref
        return None

    async def _resolve_preferred_channel_conversation(self, task: TaskModel) -> str | None:
        async with self._session_factory() as db_session:
            account = await get_preferred_channel_account_for_agent(
                db_session,
                user_email=task.created_by,
                agent_id=task.agent_id,
            )
            if account is not None:
                if account.default_conversation_id:
                    conversation = await get_conversation(
                        db_session, account.default_conversation_id
                    )
                    route = await get_conversation_channel_route(
                        db_session, account.default_conversation_id
                    )
                    if (
                        conversation is not None
                        and conversation.status == "active"
                        and conversation.user_email == task.created_by
                        and conversation.agent_id == task.agent_id
                        and route is not None
                        and route[1] == account.account_id
                    ):
                        return conversation.conversation_id
                latest = await get_latest_active_conversation_for_channel_account(
                    db_session,
                    user_email=task.created_by,
                    agent_id=task.agent_id,
                    account_id=account.account_id,
                    prefer_unthreaded=True,
                )
                if latest is not None:
                    return latest.conversation_id

            direct = await get_agent_direct_conversation(db_session, task.created_by, task.agent_id)
            if direct is not None:
                return direct.conversation_id

        if hasattr(self._session_manager, "get_or_create_agent_direct_conversation"):
            direct_model = await self._session_manager.get_or_create_agent_direct_conversation(
                user_email=task.created_by,
                agent_id=task.agent_id,
            )
            return direct_model.conversation_id
        return None

    async def _deliver_task_result_direct(
        self, task: TaskModel, target_conversation_id: str
    ) -> None:
        """Deliver the final workflow deliverable directly to the resolved channel."""

        final_content = ""
        attachments: list[dict[str, Any]] = []
        final_deliverable_id: str | None = None
        deliverable_already_delivered = False
        if isinstance(task.result_data, dict):
            raw_format = task.result_data.get("final_format")
            final_format = raw_format if isinstance(raw_format, str) else None
            raw_deliverable_id = task.result_data.get("final_deliverable_id")
            if isinstance(raw_deliverable_id, str) and raw_deliverable_id:
                async with self._session_factory() as db_session:
                    deliverable_row = await get_deliverable(db_session, raw_deliverable_id)
                if deliverable_row is not None:
                    final_deliverable_id = deliverable_row.deliverable_id
                    deliverable_already_delivered = (
                        deliverable_row.status == DeliverableStatus.DELIVERED
                    )
            raw_content = None
            if final_deliverable_id is None:
                raw_content = task.result_data.get("final_channel_content")
            if not raw_content and (final_deliverable_id is not None or final_format != "html"):
                raw_content = task.result_data.get("final_content")
            if not final_content and isinstance(raw_content, str):
                final_content = raw_content.strip()
            raw_attachments = task.result_data.get("attachments")
            if isinstance(raw_attachments, list):
                attachments = [item for item in raw_attachments if isinstance(item, dict)]

        if not final_content and final_deliverable_id is not None:
            final_content = self._build_task_delivery_fallback(task)

        if not final_content:
            logger.warning(
                "task_delivery: direct delivery requested but final content missing; falling back",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            task.applied_completion_mode = "default"
            task.applied_completion_reason = (
                "Direct delivery requested but no final assistant message was available."
            )
            await self._update_applied_completion_fields(task)
            await self._deliver_task_result_default(task, target_conversation_id)
            return

        if self._channel_delivery is None:
            logger.warning(
                "task_delivery: direct delivery unavailable; channel delivery service missing",
                extra={"extra_data": {"task_id": task.task_id}},
            )
            task.applied_completion_mode = "default"
            task.applied_completion_reason = (
                "Direct delivery fell back because channel delivery is unavailable."
            )
            await self._update_applied_completion_fields(task)
            await self._deliver_task_result_default(task, target_conversation_id)
            return

        async with self._session_factory() as db_session:
            from cognis.store.queries import get_conversation_channel_route

            route = await get_conversation_channel_route(db_session, target_conversation_id)
        if route is None:
            logger.warning(
                "task_delivery: direct delivery requested but no channel route resolved; falling back",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "conversation_id": target_conversation_id,
                    }
                },
            )
            task.applied_completion_mode = "default"
            task.applied_completion_reason = (
                "Direct delivery fell back because no direct-capable channel target was resolved."
            )
            await self._update_applied_completion_fields(task)
            await self._deliver_task_result_default(task, target_conversation_id)
            return

        if deliverable_already_delivered and final_deliverable_id is not None:
            logger.warning(
                "task_delivery: deliverable already marked delivered, skipping duplicate direct send",
                extra={
                    "extra_data": {"task_id": task.task_id, "deliverable_id": final_deliverable_id}
                },
            )
            await self._publish_task_terminal_event(
                task,
                conversation_id=target_conversation_id,
                direct_delivery=True,
                delivery_skipped_reason="deliverable_already_delivered",
            )
            return

        from cognis.channels.delivery import ChannelDeliveryStatus

        delivery_status = await self._channel_delivery.deliver_task_to_conversation(
            target_conversation_id,
            task_id=task.task_id,
            content=final_content,
            attachments=attachments,
            deliverable_id=final_deliverable_id,
        )
        if delivery_status != ChannelDeliveryStatus.SENT:
            logger.warning(
                "task_delivery: direct delivery remains incomplete",
                extra={
                    "extra_data": {
                        "task_id": task.task_id,
                        "delivery_status": delivery_status,
                    }
                },
            )
            task.applied_completion_reason = (
                "Direct channel delivery is pending durable retry."
                if delivery_status != ChannelDeliveryStatus.UNCERTAIN
                else "Direct channel delivery outcome is uncertain and requires reconciliation."
            )
            await self._update_applied_completion_fields(task)
            await self._publish_task_terminal_event(
                task,
                conversation_id=target_conversation_id,
                direct_delivery=True,
                channel_delivery_status=delivery_status,
            )
            return

        logger.info(
            "task_delivery: direct delivery sent",
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

        await self._event_bus.publish(
            Event(
                type=event_type,
                data={
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "title": task.title,
                    "conversation_id": target_conversation_id,
                    "result_summary": task.result_summary,
                    "attachments": (task.result_data or {}).get("attachments", []),
                    "direct_delivery": True,
                },
            )
        )

    async def _update_applied_completion_fields(self, task: TaskModel) -> None:
        async with self._session_factory() as db_session:
            from cognis.store.queries import get_task

            row = await get_task(db_session, task.task_id)
            if row is None:
                return
            row.applied_completion_mode = task.applied_completion_mode
            row.applied_completion_reason = task.applied_completion_reason
            await db_session.commit()

    async def _deliver_task_result_default(
        self, task: TaskModel, target_conversation_id: str
    ) -> None:
        """Deliver task results through the normal follow-up flow."""

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
                "attachments": (task.result_data or {}).get("attachments", []),
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

                    if sess is not None:
                        delivery_session_id = sess.session_id
                    if sess and sess.intaris_session_id:
                        await self._providers.guardrails.record_events(
                            session_id=sess.intaris_session_id,
                            events=with_session_events_turn_id([event], None),
                            source="cognis",
                            # Idempotency key: the provider retries internally
                            # and this loop retries once more — a lost response
                            # after a successful server-side append must not
                            # duplicate the task-result message on reload.
                            idempotency_key=(
                                f"{sess.intaris_session_id}:task_delivery:"
                                f"{task.task_id}:{task.status}"
                            ),
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
        async with self._session_factory() as db_session:
            await mark_conversation_unread(db_session, target_conversation_id)
            await db_session.commit()

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
                    delivery_fallback_text = self._build_task_delivery_fallback(task)
                    await create_channel_delivery_outbox(
                        db_session,
                        delivery_id=delivery_id,
                        user_email=user_email,
                        conversation_id=target_conversation_id,
                        session_id=delivery_session_id,
                        source_type="task_result_follow_up",
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
                    "attachments": (task.result_data or {}).get("attachments", []),
                    "channel_follow_up_delivery_id": delivery_id,
                },
            )
        )

        # Always request a follow-up turn so the agent can process the
        # result even if Intaris recording failed (degraded mode).
        follow_up = await self._follow_up_policy.build_task_result_follow_up(
            conversation_id=target_conversation_id,
            task_id=task.task_id,
            task_title=task.title,
            status=str(task.status),
            source_type=task.source_type,
            delivery_mode=task.delivery.mode,
            result_summary=task.result_summary,
            description=task.description,
            session_id=delivery_session_id,
            session_cache=self._session_cache,
        )
        await self._event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": target_conversation_id,
                    "origin_session_id": task.source_session_id,
                    "follow_up": follow_up.model_dump(mode="json"),
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
            await assert_task_execution_fence(db_session)
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
            await assert_task_execution_fence(db_session)
            updated = await update_task_status(
                db_session,
                task.task_id,
                task.status,
                completed_at=task.completed_at,
                result_summary=task.result_summary,
                result_data=task.result_data,
                applied_completion_mode=task.applied_completion_mode,
                applied_completion_reason=task.applied_completion_reason,
                delivery_mode=task.delivery.mode,
            )
            if not updated:
                await db_session.rollback()
                authoritative = await self._read_task_status(task.task_id)
                task.status = authoritative
                raise StepInterrupted(task.task_id)
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

    @staticmethod
    def _session_needed_by_future_reuse(
        workflow: Workflow,
        state: WorkflowState,
        *,
        current_step_index: int,
        current_step_name: str,
        session_id: str,
    ) -> bool:
        """Return whether a later static step can continue this physical session."""

        for future_step in workflow.steps[current_step_index + 1 :]:
            reuse_source = (
                future_step.input.reuse_session_from if future_step.input is not None else None
            )
            if reuse_source is None:
                continue
            if reuse_source == current_step_name:
                return True
            raw_output = state.step_outputs.get(reuse_source)
            if isinstance(raw_output, dict) and raw_output.get("session_id") == session_id:
                return True
        return False

    @staticmethod
    def _session_needed_by_reuse_from_index(
        workflow: Workflow,
        state: WorkflowState,
        *,
        start_step_index: int,
        session_id: str,
    ) -> bool:
        for future_step in workflow.steps[start_step_index:]:
            reuse_source = (
                future_step.input.reuse_session_from if future_step.input is not None else None
            )
            if reuse_source is None:
                continue
            raw_output = state.step_outputs.get(reuse_source)
            if isinstance(raw_output, dict) and raw_output.get("session_id") == session_id:
                return True
        return False

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
        *,
        copy_prefix: bool = True,
    ) -> bool:
        """Copy events from a source step's session into the target session.

        This implements the ``type="full"`` fork behaviour: the new step
        session starts with the source step's conversation as natural
        history.  Events are written to Intaris (durable) and seeded into
        the session cache (avoids a cold load on context assembly).

        Failures are logged but do not block step execution — the step
        prompt still includes a structured summary from
        ``_build_step_prompt`` as a fallback.
        """
        if source_name is None:
            return False

        raw_output = state.step_outputs.get(source_name, {})
        return await self._fork_session_events(
            source_cognis_session_id=raw_output.get("session_id"),
            source_intaris_session_id=raw_output.get("intaris_session_id"),
            target_session=target_session,
            source_label=source_name,
            copy_prefix=copy_prefix,
        )

    async def _fork_session_events(
        self,
        *,
        source_cognis_session_id: str | None,
        source_intaris_session_id: str | None,
        target_session: Any,
        source_label: str,
        copy_prefix: bool = True,
        event_filter: Callable[[Any], bool] | None = None,
        event_transform: Callable[[Any], Any | None] | None = None,
        prefer_durable_source: bool = False,
    ) -> bool:
        """Copy events from one session into another session."""
        kwargs: dict[str, Any] = {}
        if event_filter is not None:
            kwargs["event_filter"] = event_filter
        if event_transform is not None:
            kwargs["event_transform"] = event_transform
        if prefer_durable_source:
            kwargs["prefer_durable_source"] = True
        return await fork_session_events(
            providers=self._providers,
            session_cache=self._session_cache,
            source_cognis_session_id=source_cognis_session_id,
            source_intaris_session_id=source_intaris_session_id,
            target_session=target_session,
            source_label=source_label,
            snapshot_source="fork",
            snapshot_extras={"source_step": source_label},
            copy_prefix=copy_prefix,
            **kwargs,
        )

    async def _resolve_step_runtime(
        self,
        *,
        agent: AgentDefinition,
        user_email: str,
        executor_agent: AgentDefinition | None = None,
        access_context: RuntimeAccessContext | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
    ) -> ResolvedStepRuntime:
        """Resolve the tool registry and executor connection for one step/turn."""
        if callable(self._step_runtime_factory):
            attempts: list[dict[str, Any]] = [
                {
                    "agent": agent,
                    "user_email": user_email,
                    "executor_agent": executor_agent,
                    "access_context": access_context,
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                },
                # Compat: factory without task_id
                {
                    "agent": agent,
                    "user_email": user_email,
                    "executor_agent": executor_agent,
                    "access_context": access_context,
                    "conversation_id": conversation_id,
                },
                # Compat: factory without conversation_id
                {
                    "agent": agent,
                    "user_email": user_email,
                    "executor_agent": executor_agent,
                    "access_context": access_context,
                },
                # Compat: factory without access_context
                {
                    "agent": agent,
                    "user_email": user_email,
                    "executor_agent": executor_agent,
                },
            ]
            last_exc: TypeError | None = None
            for kwargs in attempts:
                try:
                    return cast(
                        ResolvedStepRuntime,
                        await self._step_runtime_factory(**kwargs),
                    )
                except TypeError as exc:
                    msg = str(exc)
                    last_exc = exc
                    # Only retry if the error is about an unexpected kwarg.
                    if "unexpected keyword" not in msg and "got an unexpected" not in msg:
                        raise
            if last_exc is not None:
                raise last_exc

        raise RuntimeError("Step runtime factory unavailable; refusing shared executor fallback")

    def _executor_agent_for_step(
        self,
        primary_agent: AgentDefinition,
        step_agent: AgentDefinition,
    ) -> AgentDefinition:
        """Return the agent whose executor is eligible for this step."""

        if step_agent.is_system or step_agent.agent_type == "secondary":
            return primary_agent
        return step_agent

    async def _resolve_step_agents(
        self,
        task: TaskModel,
        step_def: StepDefinition,
    ) -> tuple[AgentDefinition | None, AgentDefinition | None]:
        """Resolve which agent runs a step.

        If the step has ``agent_override``, resolve that agent (checking
        the AgentRegistry for system agents first, then DB). Otherwise,
        resolve the task's primary agent.
        """
        from cognis.core.agent_registry import AgentRegistry
        from cognis.store.queries import get_active_agent_grant

        registry = AgentRegistry(self._session_factory)
        primary_agent = await registry.get(task.agent_id, owner_email=task.created_by)
        if primary_agent is None or primary_agent.status != "active":
            return None, None

        if step_def.agent_override:
            override_agent = await registry.get(
                step_def.agent_override, owner_email=task.created_by
            )
            if override_agent is None or override_agent.status != "active":
                logger.warning(
                    "agent_override agent not found or inactive",
                    extra={
                        "extra_data": {
                            "agent_override": step_def.agent_override,
                            "task_id": task.task_id,
                            "step_name": step_def.name,
                        }
                    },
                )
                return None, None

            if not override_agent.is_system and override_agent.owner_email != task.created_by:
                async with self._session_factory() as db_session:
                    grant = await get_active_agent_grant(
                        db_session,
                        override_agent.agent_id,
                        task.created_by,
                    )
                if grant is None:
                    logger.warning(
                        "agent_override agent is not accessible to task owner",
                        extra={
                            "extra_data": {
                                "agent_override": step_def.agent_override,
                                "task_id": task.task_id,
                            }
                        },
                    )
                    return None, None

            if override_agent.agent_type == "secondary":
                is_bound = await registry.is_secondary_bound(
                    primary_agent.agent_id,
                    override_agent.agent_id,
                )
                if not is_bound:
                    logger.warning(
                        "agent_override secondary is not bound to primary agent",
                        extra={
                            "extra_data": {
                                "agent_override": step_def.agent_override,
                                "primary_agent_id": primary_agent.agent_id,
                                "task_id": task.task_id,
                            }
                        },
                    )
                    return None, None

            return primary_agent, override_agent

        # Default: use the task's primary agent
        return primary_agent, primary_agent

    async def _create_step_session(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        agent: AgentDefinition,
        agent_profile_id: str | None = None,
        session_policy: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Create a conversation and session for a workflow step.

        Stage 36: copies the task-level ``active_executor_id`` pin (if any)
        into the new step conversation so all steps of a task run on the
        same executor unless the agent or user explicitly switches.
        """
        from cognis.models.session import ConversationContext

        workspace_root, working_directory = _resolve_task_execution_paths(task)
        context = ConversationContext(
            type="task",
            ref=task.task_id,
            platform_data={
                "workspace_root": workspace_root,
                "working_directory": working_directory,
            },
        )
        # Stage 36: read the task-level active executor pin so it carries
        # forward to this step's conversation.
        task_active_executor_id: str | None = None
        task_active_executor_assigned_at = None
        task_active_executor_expires_at = None
        task_active_executor_source = None
        try:
            async with self._session_factory() as db_session:
                from cognis.store.queries import get_task

                task_row = await get_task(db_session, task.task_id)
                if task_row is not None:
                    task_active_executor_id = getattr(task_row, "active_executor_id", None)
                    task_active_executor_assigned_at = getattr(
                        task_row, "active_executor_assigned_at", None
                    )
                    task_active_executor_expires_at = getattr(
                        task_row, "active_executor_expires_at", None
                    )
                    task_active_executor_source = getattr(task_row, "active_executor_source", None)
        except Exception:
            logger.debug(
                "stage36: failed to read task.active_executor_id",
                exc_info=True,
            )
        with scoped_runtime_context(
            workspace_root=workspace_root,
            effective_working_directory=working_directory,
            access_context=RuntimeAccessContext(
                user_email=task.created_by,
                agent_id=agent.agent_id,
                agent_owner_email=agent.owner_email,
                agent_type=agent.agent_type,
                session_id=None,
                task_id=task.task_id,
                step_name=step_def.name,
                workflow_step=True,
                session_policy=session_policy or {},
            ),
        ):
            (
                conversation,
                session,
            ) = await self._session_manager.create_conversation_with_root_session(
                user_email=task.created_by,
                agent_id=agent.agent_id,
                agent_profile_id=agent_profile_id,
                context=context,
                title=f"Task: {task.title} / Step: {step_def.name}",
                title_source="manual",
                intention=f"Task: {task.title} — Step: {step_def.name} — {step_def.description or step_def.prompt[:100]}",
                initial_active_executor_id=task_active_executor_id,
                initial_active_executor_assigned_at=task_active_executor_assigned_at,
                initial_active_executor_expires_at=task_active_executor_expires_at,
                initial_active_executor_source=task_active_executor_source,
                project_id=task.project_id,
            )
        return conversation, session

    async def _reuse_or_create_step_session(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        agent: AgentDefinition,
        agent_profile_id: str | None = None,
        session_policy: dict[str, Any] | None = None,
        require_same_conversation: bool = False,
    ) -> tuple[Any, Any, bool]:
        """Reuse the prior step session on retry, or create a new one.

        On retry, the spec requires continuing the same Intaris session
        so the agent keeps its prior work and sees evaluation feedback.
        """
        try:
            async with self._session_factory() as db_session:
                prior_run = await get_latest_step_run_for_task_step(
                    db_session,
                    task.task_id,
                    step_def.name,
                    attempt_number=task.attempt_number,
                    current_revision_only=True,
                    eligible_statuses={"approved", "rejected", "failed", "running", "paused"},
                )
            if prior_run is not None and prior_run.session_id is not None:
                # Recover session and conversation from the prior run
                from cognis.core.session import _to_conversation_model, _to_session_model
                from cognis.store.queries import get_conversation, get_session_row

                async with self._session_factory() as db_session:
                    session_row = await get_session_row(db_session, prior_run.session_id)
                    if session_row is not None and self._is_reusable_step_session_status(
                        session_row.status
                    ):
                        conv_row = await get_conversation(db_session, session_row.conversation_id)
                        if conv_row is not None:
                            conversation = _to_conversation_model(conv_row)
                            session = _to_session_model(session_row)
                            return conversation, session, True
                    if session_row is not None:
                        conv_row = await get_conversation(db_session, session_row.conversation_id)
                        if conv_row is not None:
                            conversation = _to_conversation_model(conv_row)
                            with scoped_runtime_context(
                                access_context=RuntimeAccessContext(
                                    user_email=task.created_by,
                                    agent_id=agent.agent_id,
                                    agent_owner_email=agent.owner_email,
                                    agent_type=agent.agent_type,
                                    session_id=None,
                                    conversation_id=session_row.conversation_id,
                                    task_id=task.task_id,
                                    step_name=step_def.name,
                                    step_run_id=prior_run.step_run_id,
                                    workflow_step=True,
                                    session_policy=session_policy or {},
                                )
                            ):
                                resumed_session = await self._session_manager.create_root_session(
                                    conversation_id=session_row.conversation_id,
                                    user_email=task.created_by,
                                    agent_id=agent.agent_id,
                                    agent_profile_id=agent_profile_id,
                                    intention=(
                                        f"Task: {task.title} — Step: {step_def.name} — "
                                        f"{step_def.description or step_def.prompt[:100]}"
                                    ),
                                )
                            seeded_from_prior = await self._fork_session_events(
                                source_cognis_session_id=prior_run.session_id,
                                source_intaris_session_id=(
                                    prior_run.intaris_session_id or session_row.intaris_session_id
                                ),
                                target_session=resumed_session,
                                source_label=f"{step_def.name}:resume",
                            )
                            if require_same_conversation and not seeded_from_prior:
                                raise RuntimeError(
                                    f"Could not recover continued step session "
                                    f"for {step_def.name!r}"
                                )
                            return conversation, resumed_session, seeded_from_prior
            if require_same_conversation:
                raise RuntimeError(
                    f"Continued step {step_def.name!r} has no recoverable prior conversation"
                )
        except Exception:
            if require_same_conversation:
                raise
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
        conversation, session = await self._create_step_session(
            task,
            step_def,
            agent,
            agent_profile_id=agent_profile_id,
            session_policy=session_policy,
        )
        return conversation, session, False

    async def _current_input_source_runs(
        self,
        task: TaskModel,
        source_names: list[str],
    ) -> dict[str, Any]:
        """Return current approved source rows for this task attempt."""

        rows: dict[str, Any] = {}
        async with self._session_factory() as db_session:
            for source_name in source_names:
                row = await get_latest_approved_step_run_for_task_step(
                    db_session,
                    task.task_id,
                    source_name,
                    attempt_number=task.attempt_number,
                )
                if row is not None:
                    rows[source_name] = row
        return rows

    def _session_last_event_seq(self, session_id: str) -> int:
        if self._session_cache is None:
            return 0
        entry = self._session_cache.get_entry(session_id)
        return int(getattr(entry, "last_event_seq", 0) or 0)

    def _step_prompt_boundary_recorded(self, session_id: str, step_run_id: str) -> bool:
        if self._session_cache is None:
            return False
        entry = self._session_cache.get_entry(session_id)
        if entry is None:
            return False
        return any(
            event.type == "user_message"
            and isinstance(event.data, dict)
            and event.data.get("turn_id") == step_run_id
            for event in entry.events
        )

    async def _refresh_reused_session_authoritatively(
        self,
        session: Any,
        step_name: str,
    ) -> Any:
        entry = await self._session_cache.refresh(session)
        if entry is None:
            raise RuntimeError(
                f"Cannot establish an evidence boundary for reused step {step_name!r}"
            )
        durable_session_id = session.intaris_session_id or session.session_id
        probe = await self._providers.guardrails.read_events(
            session_id=durable_session_id,
            after_seq=entry.last_event_seq,
            allow_missing_stream=False,
        )
        durable_events = [
            event for event in list(getattr(probe, "events", []) or []) if isinstance(event, dict)
        ]
        if durable_events:
            entry = await self._session_cache.refresh(session)
            max_seq = max(int(event.get("seq", 0) or 0) for event in durable_events)
            if entry.last_event_seq < max_seq:
                raise RuntimeError(
                    f"Cannot establish an authoritative evidence boundary "
                    f"for reused step {step_name!r}"
                )
        return entry

    async def _reuse_source_step_session(
        self,
        task: TaskModel,
        step_def: StepDefinition,
        agent: AgentDefinition,
        *,
        source_name: str,
        agent_profile_id: str | None,
        session_policy: dict[str, Any] | None,
    ) -> tuple[Any, Any, Any, dict[str, Any]]:
        """Continue an approved source step session without creating a new conversation."""

        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.store.queries import get_conversation, get_session_row

        async with self._session_factory() as db_session:
            source_run = await get_latest_approved_step_run_for_task_step(
                db_session,
                task.task_id,
                source_name,
                attempt_number=task.attempt_number,
            )
            if source_run is None:
                raise RuntimeError(
                    f"No current approved StepRun is available for reuse source {source_name!r}"
                )
            if source_run.agent_id != agent.agent_id:
                raise RuntimeError(
                    f"Cannot reuse step {source_name!r}: resolved agent differs from "
                    f"target step {step_def.name!r}"
                )
            if source_run.agent_profile_id != agent_profile_id:
                raise RuntimeError(
                    f"Cannot reuse step {source_name!r}: resolved runtime profile differs from "
                    f"target step {step_def.name!r}"
                )

            source_session_row = (
                await get_session_row(db_session, source_run.session_id)
                if source_run.session_id
                else None
            )
            conversation_id = source_run.conversation_id or getattr(
                source_session_row, "conversation_id", None
            )
            if not conversation_id:
                raise RuntimeError(f"Reuse source {source_name!r} has no recoverable conversation")
            conversation_row = await get_conversation(db_session, conversation_id)
            if conversation_row is None:
                raise RuntimeError(f"Reuse source {source_name!r} conversation no longer exists")

            active_session_row = None
            active_session_id = getattr(conversation_row, "active_session_id", None)
            if active_session_id:
                active_session_row = await get_session_row(db_session, active_session_id)
            candidate = active_session_row or source_session_row
            if candidate is not None and self._is_reusable_step_session_status(candidate.status):
                reason = (
                    "conversation_active_session"
                    if candidate.session_id != source_run.session_id
                    else "reattached"
                )
                return (
                    _to_conversation_model(conversation_row),
                    _to_session_model(candidate),
                    source_run,
                    {
                        "reason": reason,
                        "source_session_id": source_run.session_id,
                        "selected_session_id": candidate.session_id,
                    },
                )

        seed_row = candidate or source_session_row
        with scoped_runtime_context(
            access_context=RuntimeAccessContext(
                user_email=task.created_by,
                agent_id=agent.agent_id,
                agent_owner_email=agent.owner_email,
                agent_type=agent.agent_type,
                session_id=None,
                conversation_id=conversation_id,
                task_id=task.task_id,
                step_name=step_def.name,
                step_run_id=source_run.step_run_id,
                workflow_step=True,
                session_policy=session_policy or {},
            )
        ):
            resumed_session = await self._session_manager.create_root_session(
                conversation_id=conversation_id,
                user_email=task.created_by,
                agent_id=agent.agent_id,
                agent_profile_id=agent_profile_id,
                intention=(
                    f"Task: {task.title} — Step: {step_def.name} — "
                    f"{step_def.description or step_def.prompt[:100]}"
                ),
            )
        seeded = await self._fork_session_events(
            source_cognis_session_id=getattr(seed_row, "session_id", None) or source_run.session_id,
            source_intaris_session_id=getattr(seed_row, "intaris_session_id", None)
            or source_run.intaris_session_id,
            target_session=resumed_session,
            source_label=f"{source_name}:reuse_recovery",
            event_filter=lambda event: event.type not in {"assistant_thinking", "reasoning"},
            event_transform=self._sanitize_reuse_recovery_event,
            prefer_durable_source=True,
        )
        if not seeded:
            raise RuntimeError(
                f"Could not recover reusable context for source step {source_name!r}"
            )
        return (
            _to_conversation_model(conversation_row),
            resumed_session,
            source_run,
            {
                "reason": "rotated_uncontinuable_session",
                "source_session_id": getattr(seed_row, "session_id", None) or source_run.session_id,
                "selected_session_id": resumed_session.session_id,
            },
        )

    @staticmethod
    def _sanitize_reuse_recovery_event(event: Any) -> Any | None:
        """Remove provider-private continuation state from copied history."""

        if event.type in {"assistant_thinking", "reasoning"}:
            return None
        data = dict(event.data or {})
        for key in (
            "responses_output_items",
            "anthropic_native_envelope",
            "provider_thinking_blocks",
            "thinking_blocks",
            "reasoning_content",
            "reasoning",
        ):
            data.pop(key, None)
        return replace(event, data=data)

    @staticmethod
    def _todos_for_evaluation_retry(
        state: WorkflowState,
        persisted_todos: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return active todos for a feedback-driven retry attempt.

        A rejected attempt may have completed all todos before calling
        step_complete. Reusing that terminal list would put the retry straight
        back into finalization-only mode, preventing the revision from doing
        normal work. Preserve the previous todo history on the rejected step
        run, but reopen the active retry with one pending revision todo.
        """

        terminal_statuses = {"completed", "cancelled", "done"}
        if not persisted_todos or not all(
            str(todo.get("status") or "pending") in terminal_statuses for todo in persisted_todos
        ):
            return persisted_todos

        if state.last_retry_reason == "routed_revision":
            content = "Revise this step based on the routed independent review."
            feedback = (state.last_revision_context or "").strip()
        else:
            content = "Revise the step output based on evaluator feedback."
            feedback = (state.last_evaluation_feedback or "").strip()
        if feedback:
            content = f"{content} Feedback: {feedback}"
        return [{"content": content, "status": "pending"}]

    async def _has_prior_step_session(
        self,
        task: TaskModel,
        step_def: StepDefinition,
    ) -> bool:
        """Return whether the latest prior step run exists."""

        async with self._session_factory() as db_session:
            prior_run = await get_latest_step_run_for_task_step(
                db_session,
                task.task_id,
                step_def.name,
                attempt_number=task.attempt_number,
                current_revision_only=True,
                eligible_statuses={"approved", "rejected", "failed", "running", "paused"},
            )
            return prior_run is not None and prior_run.session_id is not None

    def _is_reusable_step_session_status(self, status: str | None) -> bool:
        """Return whether a step session can be safely reused."""

        return status in {"active", "idle"}

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

    @staticmethod
    def _missing_required_deliverable(
        step_def: StepDefinition,
        output: StepOutput | None,
    ) -> bool:
        """Return whether a finished run step violated its artifact contract."""

        return bool(
            output is not None and step_def.require_deliverable and not output.deliverable_id
        )

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

    def _build_failure_result_summary(self, state: WorkflowState, workflow: Workflow) -> str:
        """Build the most specific available summary for a failed workflow."""

        if workflow.steps:
            current_index = min(max(state.current_step_index, 0), len(workflow.steps) - 1)
            current_step = workflow.steps[current_index]
            raw_output = state.step_outputs.get(current_step.name)
            if raw_output and isinstance(raw_output, dict):
                summary = str(raw_output.get("summary", "")).strip()
                if summary:
                    return summary
                error = str(raw_output.get("error", "")).strip()
                if error:
                    return error

        return self._build_result_summary(state, workflow)

    def _build_task_delivery_fallback(self, task: TaskModel) -> str:
        """Build a bounded channel fallback that identifies the task and status."""

        status_label = {
            TaskStatus.COMPLETED: "completed",
            TaskStatus.FAILED: "failed",
            TaskStatus.CANCELLED: "was cancelled",
        }.get(task.status, f"changed status to {task.status.value}")
        title = self._truncate_channel_fallback_field(task.title, max_length=180)
        lines = [f'Task "{title}" {status_label}.']

        summary = self._truncate_channel_fallback_field(task.result_summary or "", max_length=500)
        if summary:
            label = (
                "Reason" if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} else "Summary"
            )
            lines.extend(["", f"{label}: {summary}"])

        lines.extend(["", f"Task ID: {task.task_id}", "", "Open the conversation for details."])
        return "\n".join(lines)

    @staticmethod
    def _truncate_channel_fallback_field(value: str, *, max_length: int) -> str:
        """Normalize and bound text embedded into channel fallback messages."""

        normalized = re.sub(r"\s+", " ", value).strip()
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 1].rstrip() + "…"

    def _channel_safe_deliverable_content(self, content: str, format_name: str) -> str:
        """Render deliverable content into a channel-safe text form."""

        if format_name != "html":
            return content
        stripped = re.sub(r"<[^>]+>", " ", content)
        normalized = re.sub(r"\n{3,}", "\n\n", stripped)
        normalized = re.sub(r"[ \t]{2,}", " ", normalized)
        return html.unescape(normalized).strip()

    async def _build_result_data(
        self,
        task: TaskModel,
        state: WorkflowState,
        workflow: Workflow,
    ) -> dict[str, Any] | None:
        result: dict[str, Any] = {}
        if task.status == TaskStatus.COMPLETED:
            final_deliverable = await self._resolve_final_deliverable(task, state, workflow)
            if final_deliverable is not None and final_deliverable.content.strip():
                channel_content = self._channel_safe_deliverable_content(
                    final_deliverable.content,
                    str(final_deliverable.format),
                )
                summary = self._truncate_channel_fallback_field(channel_content, max_length=2000)
                result["final_deliverable_id"] = final_deliverable.deliverable_id
                result["final_content"] = summary
                result["final_content_summary"] = summary
                result["final_format"] = final_deliverable.format
                if final_deliverable.title:
                    result["final_title"] = final_deliverable.title
            else:
                last_output = self._last_step_output(state)
                if last_output is not None and last_output.content.strip():
                    summary = self._truncate_channel_fallback_field(
                        last_output.content,
                        max_length=2000,
                    )
                    result["final_content"] = summary
                    result["final_content_summary"] = summary
                    result["final_format"] = "markdown"
                    result["final_channel_content"] = self._channel_safe_deliverable_content(
                        last_output.content,
                        "markdown",
                    )

        attachment_source: StepOutput | None = None
        if task.status == TaskStatus.COMPLETED and "final_deliverable_id" in result:
            final_deliverable_id = str(result["final_deliverable_id"])
            for raw in reversed(list(state.step_outputs.values())):
                if not isinstance(raw, dict):
                    continue
                if raw.get("deliverable_id") != final_deliverable_id:
                    continue
                try:
                    attachment_source = StepOutput.model_validate(raw)
                except Exception:
                    continue
                break
        if attachment_source is None:
            attachment_source = self._last_step_output(state)
        attachments = list(attachment_source.attachments) if attachment_source is not None else []
        if not attachments:
            return result or None
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in attachments:
            artifact_id = str(item.get("artifact_id") or "")
            url = str(item.get("url") or "")
            key = (artifact_id, url)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        if deduped:
            result["attachments"] = deduped
        return result or None

    async def _resolve_final_deliverable(
        self,
        task: TaskModel,
        state: WorkflowState,
        workflow: Workflow,
    ) -> Deliverable | None:
        """Return the final approved deliverable for a workflow, if any."""

        for step in reversed(workflow.steps):
            if step.type != "run" or not step.require_deliverable:
                continue

            raw_output = state.step_outputs.get(step.name)
            step_executed = isinstance(raw_output, dict)
            deliverable_id = (
                raw_output.get("deliverable_id") if isinstance(raw_output, dict) else None
            )
            if isinstance(deliverable_id, str) and deliverable_id:
                async with self._session_factory() as db_session:
                    row = await get_deliverable(db_session, deliverable_id)
                if row is not None and row.status in {
                    DeliverableStatus.APPROVED,
                    DeliverableStatus.DELIVERED,
                }:
                    artifact_store = getattr(self._agent_loop, "artifact_store", None)
                    if artifact_store is not None:
                        await hydrate_deliverable_payload(row, artifact_store)
                    return Deliverable.model_validate(
                        {
                            "deliverable_id": row.deliverable_id,
                            "step_run_id": row.step_run_id,
                            "version": row.version,
                            "content": row.content,
                            "format": row.format,
                            "title": row.title,
                            "target": row.target,
                            "outputs": row.outputs or {},
                            "status": row.status,
                            "evaluator_feedback": row.evaluator_feedback,
                            "created_at": row.created_at,
                            "updated_at": row.updated_at,
                        }
                    )

            async with self._session_factory() as db_session:
                prior_run = await get_latest_step_run_for_task_step(
                    db_session,
                    task.task_id,
                    step.name,
                    attempt_number=task.attempt_number,
                    current_revision_only=True,
                    eligible_statuses={"approved"},
                )
                if prior_run is None:
                    if step_executed:
                        return None
                    continue
                row = await get_latest_approved_deliverable_for_step_run(
                    db_session, prior_run.step_run_id
                )
            if row is not None:
                artifact_store = getattr(self._agent_loop, "artifact_store", None)
                if artifact_store is not None:
                    await hydrate_deliverable_payload(row, artifact_store)
                return Deliverable.model_validate(
                    {
                        "deliverable_id": row.deliverable_id,
                        "step_run_id": row.step_run_id,
                        "version": row.version,
                        "content": row.content,
                        "format": row.format,
                        "title": row.title,
                        "target": row.target,
                        "outputs": row.outputs or {},
                        "status": row.status,
                        "evaluator_feedback": row.evaluator_feedback,
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                    }
                )
            if step_executed:
                # Do not hide a missing final artifact by promoting an earlier
                # planning deliverable. A skipped conditional step has no
                # output and can still fall through to the prior delivering
                # step that actually ran.
                return None
        return None

    def _last_step_output(self, state: WorkflowState) -> StepOutput | None:
        for raw in reversed(list(state.step_outputs.values())):
            if not isinstance(raw, dict):
                continue
            try:
                return StepOutput.model_validate(raw)
            except Exception:
                continue
        return None

    def _deterministic_completion_delivery_override(
        self,
        state: WorkflowState,
    ) -> str | None:
        last_output = self._last_step_output(state)
        if last_output is None or last_output.metadata.get("step_type") != "complete":
            return None
        override = last_output.metadata.get("delivery_mode_override")
        if override in {
            "same_conversation",
            "preferred_channel",
            "latest_active_for_agent",
            "specific_conversation",
            "silent",
        }:
            return str(override)
        return None

    def _resolve_applied_completion(
        self, task: TaskModel, state: WorkflowState
    ) -> tuple[str, str | None]:
        last_output = self._last_step_output(state)
        final_notification = last_output.notification if last_output is not None else None
        if (
            task.status == TaskStatus.COMPLETED
            and final_notification is not None
            and final_notification.mode == "silent"
        ):
            return "silent", final_notification.reason
        if (
            task.status == TaskStatus.COMPLETED
            and final_notification is not None
            and final_notification.mode == "direct"
        ):
            return "direct", final_notification.reason

        policy = task.completion_delivery or CompletionDeliveryPolicy()
        if (
            task.status == TaskStatus.COMPLETED
            and policy.allow_silent_completion
            and final_notification is None
        ):
            return (
                "silent",
                "Auto-silent completion: allow_silent_completion=true and no explicit notification override was requested.",
            )

        if task.status == TaskStatus.COMPLETED and policy.completion_mode_family == "direct":
            if isinstance(task.result_data, dict):
                final_content = task.result_data.get("final_content")
                if isinstance(final_content, str) and final_content.strip():
                    return "direct", None
            return (
                "default",
                "Direct delivery requested but no final assistant message was available.",
            )

        return "default", None


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
            GateOption(label="Continue anyway", action="continue"),
            GateOption(label="Cancel task", action="cancel"),
        ],
    )


def _build_outcome_gate(step_def: StepDefinition, outcome_reason: str | None = None) -> Any:
    """Build a gate config for an approved step that reported a failed outcome."""

    from cognis.models.workflow import GateConfig, GateOption

    message = f"Step '{step_def.name}' completed but reported a failed outcome."
    if outcome_reason:
        message += f"\n\nReason: {outcome_reason[:500]}"
    message += "\n\nYou can retry the step or continue anyway."

    return GateConfig(
        message=message,
        input=[],
        options=[
            GateOption(label="Retry step", action=f"revise({step_def.name})"),
            GateOption(label="Continue", action="continue"),
            GateOption(label="Cancel", action="cancel"),
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
