"""Agent loop engine — the step runner.

Runs a single step as a full agentic loop: context assembly, LLM calls
with streaming, tool execution via router, and step finalization. This
is the heart of Cognis.

Used directly for main chat (Direct workflow) and by the WorkflowEngine
for multi-step background tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram

from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.events import Event, EventBus, EventType
from cognis.core.pruning import prune_tool_outputs
from cognis.core.truncation import middle_truncate
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionEvent, SessionModel
from cognis.models.tool import ToolCall, ToolResult
from cognis.models.workflow import StepDefinition, StepOutput, WorkflowState
from cognis.runtime_context import scoped_runtime_context  # noqa: F401 — used in delegation
from cognis.store.queries import get_setting_value
from cognis.tools.builtin.orchestration import (
    OrchestrationMode,
    handle_delegate_tool_call,
    is_orchestration_tool,
    is_subsession_tool,
    is_task_tool,
)

logger = get_logger(__name__)

# Prometheus metrics
STEPS_TOTAL = Counter(
    "cognis_steps_total",
    "Step executions",
    labelnames=("step_type", "status"),
)
STEP_DURATION = Histogram(
    "cognis_step_duration_seconds",
    "Step execution duration",
    labelnames=("phase",),
)
STEP_TOOL_CALLS = Counter(
    "cognis_step_tool_calls_total",
    "Tool calls per step",
    labelnames=("tool_name",),
)
STEP_REPROMPTS = Counter(
    "cognis_step_reprompts_total",
    "Re-prompts for missing step_complete",
)
DELEGATIONS_TOTAL = Counter(
    "cognis_delegations_total",
    "Sub-session delegations spawned",
    labelnames=("status",),
)
AUTO_COMPACTION_DURATION = Histogram(
    "cognis_auto_compaction_duration_seconds",
    "Duration of automatic post-turn compaction (compact + rotate + cache)",
)
AUTO_COMPACTION_TIMEOUT_SECONDS = 15

# Controller-injected tool names
STEP_COMPLETE = "step_complete"
STEP_REQUEST_INPUT = "step_request_input"
STEP_TODO_WRITE = "step_todo_write"
STEP_TODO_LIST = "step_todo_list"
CONTROLLER_TOOLS = {STEP_COMPLETE, STEP_REQUEST_INPUT, STEP_TODO_WRITE, STEP_TODO_LIST}

# Callback types
TokenCallback = Callable[[str], Coroutine[Any, Any, None]]
ToolCallCallback = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, None]]
ToolResultCallback = Callable[
    [str, str, str, bool, int | None, dict[str, Any] | None],
    Coroutine[Any, Any, None],
]

# Default limits
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_STEP_TIMEOUT_SECONDS = 600  # 10 minutes
_MAX_TOOL_DATA_BYTES = 10_240  # 10 KB truncation limit for WS events
_MAX_INTARIS_TOOL_RESULT = 50_000  # Intaris gets the middle-truncated preview
_MAX_TODO_REPROMPTS = 3  # Max re-prompts for incomplete todos before force-completing


def _truncate_tool_data(text: str) -> str:
    """Truncate tool data to a bounded size for WS events."""
    if len(text) <= _MAX_TOOL_DATA_BYTES:
        return text
    return text[:_MAX_TOOL_DATA_BYTES] + f"\n... (truncated, {len(text)} bytes total)"


def _append_tool_call_event(
    events: list[SessionEvent],
    tc: ToolCall,
) -> None:
    """Record a tool_call event to the Intaris event batch."""
    events.append(
        SessionEvent(
            type="tool_call",
            data={
                "name": tc.name,
                "call_id": tc.call_id,
                "arguments": _truncate_tool_data(json.dumps(tc.arguments, default=str)),
            },
        )
    )


def _append_tool_result_event(
    events: list[SessionEvent],
    tc: ToolCall,
    output: str,
    is_error: bool,
    duration_ms: int | None = None,
) -> None:
    """Record a tool_result event to the Intaris event batch.

    Uses the same truncation limit as the regular tools path
    (``_MAX_INTARIS_TOOL_RESULT``) for consistency with Intaris
    compaction and audit consumers.
    """
    truncated = (
        output[:_MAX_INTARIS_TOOL_RESULT] if len(output) > _MAX_INTARIS_TOOL_RESULT else output
    )
    events.append(
        SessionEvent(
            type="tool_result",
            data={
                "call_id": tc.call_id,
                "name": tc.name,
                "is_error": is_error,
                "duration_ms": duration_ms,
                "result": truncated,
                "output_size": len(output),
                "has_full_output": False,
            },
        )
    )


# ---------------------------------------------------------------------------
# Stream accumulator
# ---------------------------------------------------------------------------


class StreamAccumulator:
    """Accumulates streaming chunks into complete messages and tool calls.

    Handles LiteLLM's streaming format where tool calls arrive
    incrementally across chunks.
    """

    def __init__(self) -> None:
        self.content_parts: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, int] | None = None

    def feed(self, chunk: dict[str, Any]) -> str | None:
        """Feed a stream chunk. Returns text delta if present."""
        choices = chunk.get("choices")
        if not choices:
            # Check for usage in final chunk
            usage = chunk.get("usage")
            if usage:
                self.usage = {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            return None

        delta = choices[0].get("delta", {})

        # Text content
        text_delta: str | None = delta.get("content")
        if text_delta:
            self.content_parts.append(text_delta)

        # Tool call deltas
        tc_deltas = delta.get("tool_calls")
        if tc_deltas:
            for tc_delta in tc_deltas:
                idx = tc_delta.get("index", 0)
                if idx not in self.tool_calls:
                    self.tool_calls[idx] = {
                        "id": tc_delta.get("id", ""),
                        "name": "",
                        "arguments": "",
                    }
                entry = self.tool_calls[idx]
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]
                func = tc_delta.get("function", {})
                if func.get("name"):
                    entry["name"] = func["name"]
                if func.get("arguments"):
                    entry["arguments"] += func["arguments"]

        return text_delta

    def get_content(self) -> str:
        """Return accumulated text content."""
        return "".join(self.content_parts)

    def get_tool_calls(self) -> list[ToolCall]:
        """Return finalized ToolCall objects from accumulated deltas."""
        result = []
        for _idx, tc in sorted(self.tool_calls.items()):
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": tc["arguments"]}
            result.append(
                ToolCall(
                    call_id=tc["id"] or f"call_{uuid.uuid4().hex[:12]}",
                    name=tc["name"],
                    arguments=args,
                )
            )
        return result

    def has_tool_calls(self) -> bool:
        """Return True if any tool calls were accumulated."""
        return bool(self.tool_calls)

    def reset(self) -> None:
        """Reset for the next LLM turn."""
        self.content_parts.clear()
        self.tool_calls.clear()


# ---------------------------------------------------------------------------
# Session lock
# ---------------------------------------------------------------------------


class SessionLock:
    """Per-session async locks to prevent concurrent turns."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def acquire(self, session_id: str) -> asyncio.Lock:
        """Get or create a lock for a session and acquire it."""
        async with self._meta_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
            lock = self._locks[session_id]
        await lock.acquire()
        return lock

    def release(self, session_id: str) -> None:
        """Release a session lock."""
        lock = self._locks.get(session_id)
        if lock and lock.locked():
            lock.release()

    def evict(self, session_id: str) -> None:
        """Remove a session's lock entry."""
        self._locks.pop(session_id, None)


# ---------------------------------------------------------------------------
# Escalation / input waiter
# ---------------------------------------------------------------------------


@dataclass
class PauseResolution:
    """Resolution for a paused step (escalation, gate, or input request)."""

    decision: str  # "approve" | "deny" | "continue" | "cancel"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingPause:
    """Metadata describing a currently pending pause."""

    pause_id: str
    pause_type: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    question: str | None = None
    options: list[dict[str, Any]] | None = None
    context: dict[str, Any] | None = None
    resolved: bool = False


class PauseWaiter:
    """Synchronization mechanism for step pauses.

    The agent loop calls wait() when a step needs external input (escalation
    or step_request_input). The WebSocket handler or API route (Stage 7)
    calls resolve() with the user's response.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._resolutions: dict[str, PauseResolution] = {}
        self._pending: dict[str, PendingPause] = {}

    def register(self, pause: PendingPause) -> None:
        """Register metadata for a pause before waiting on it."""
        self._pending[pause.pause_id] = pause

    async def wait(self, pause_id: str, *, timeout: float = 300.0) -> PauseResolution:
        """Wait for a pause to be resolved. Raises TimeoutError on timeout."""
        self._pending.setdefault(
            pause_id,
            PendingPause(pause_id=pause_id, pause_type="unknown"),
        )
        event = asyncio.Event()
        self._events[pause_id] = event
        if pause_id in self._resolutions:
            event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._resolutions.pop(pause_id, PauseResolution(decision="deny"))
        finally:
            self._events.pop(pause_id, None)
            self._pending.pop(pause_id, None)

    def resolve(self, pause_id: str, resolution: PauseResolution) -> bool:
        """Resolve a waiting pause exactly once.

        Returns True when the pause was resolved by this call, False when the
        pause does not exist or has already been resolved.
        """
        pending = self._pending.get(pause_id)
        if pending is None or pending.resolved:
            return False
        pending.resolved = True
        self._resolutions[pause_id] = resolution
        event = self._events.get(pause_id)
        if event:
            event.set()
            return True
        return True

    def get(self, pause_id: str) -> PendingPause | None:
        """Return pending pause metadata."""
        return self._pending.get(pause_id)

    def find_pending(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        step_name: str | None = None,
        pause_type: str | None = None,
        include_resolved: bool = False,
    ) -> PendingPause | None:
        """Find the first unresolved pause matching the provided filters."""
        for pause in self._pending.values():
            if pause.resolved and not include_resolved:
                continue
            if task_id is not None and pause.task_id != task_id:
                continue
            if session_id is not None and pause.session_id != session_id:
                continue
            if conversation_id is not None and pause.conversation_id != conversation_id:
                continue
            if step_name is not None and pause.step_name != step_name:
                continue
            if pause_type is not None and pause.pause_type != pause_type:
                continue
            return pause
        return None

    def list_pending(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        pause_type: str | None = None,
    ) -> list[PendingPause]:
        """List unresolved pauses, optionally filtered by task, session, or conversation."""
        result: list[PendingPause] = []
        for pause in self._pending.values():
            if pause.resolved:
                continue
            if task_id is not None and pause.task_id != task_id:
                continue
            if session_id is not None and pause.session_id != session_id:
                continue
            if conversation_id is not None and pause.conversation_id != conversation_id:
                continue
            if pause_type is not None and pause.pause_type != pause_type:
                continue
            result.append(pause)
        return result

    def clear(self, pause_id: str) -> None:
        """Remove all local state for a pause.

        Used by recovered pause flows where a response is stored for later
        replay rather than being consumed by an already waiting coroutine.
        """
        self._events.pop(pause_id, None)
        self._resolutions.pop(pause_id, None)
        self._pending.pop(pause_id, None)

    def pending_count(self) -> int:
        """Return number of active waits."""
        return len([pause for pause in self._pending.values() if not pause.resolved])


class StepInterrupted(Exception):
    """Raised when a step exits early because of pause/cancel control flow."""


# ---------------------------------------------------------------------------
# Step context
# ---------------------------------------------------------------------------


@dataclass
class StepContext:
    """Runtime context for a step execution."""

    step_definition: StepDefinition
    session: SessionModel
    conversation: ConversationModel
    agent: AgentDefinition
    step_inputs: dict[str, StepOutput] = field(default_factory=dict)
    todos: list[dict[str, Any]] = field(default_factory=list)
    task_id: str | None = None
    task_title: str = ""
    task_description: str = ""
    step_run_id: str | None = None
    is_direct: bool = False  # True for main chat (Direct workflow)
    is_retry: bool = False  # True for re-attempt within the same step
    user_message: str = ""
    interaction_mode: str = "explicit_gates"
    tool_registry: Any = None  # ToolRegistry instance for this step
    executor_connection: Any = None  # ExecutorConnection for this step
    workflow_state: WorkflowState | None = None
    workflow_steps: list[StepDefinition] | None = None  # All steps for source resolution
    step_index: int = 0  # Index of current step in workflow
    cancel_event: asyncio.Event | None = None
    system_initiated: bool = False
    orchestration_mode: OrchestrationMode = OrchestrationMode.FULL


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


class AgentLoop:
    """Runs a single step as a full agentic loop."""

    def __init__(
        self,
        *,
        providers: Any,
        session_manager: Any,
        session_cache: Any,
        context_assembler: Any,
        compaction_strategy: Any,
        tool_router: Any,
        remember_queue: Any,
        event_bus: EventBus,
        session_lock: SessionLock,
        pause_waiter: PauseWaiter,
        step_context_assembler: Any = None,
        tool_output_store: Any = None,
    ) -> None:
        self.providers = providers
        self.session_manager = session_manager
        self.session_cache = session_cache
        self.context_assembler = context_assembler
        self.compaction_strategy = compaction_strategy
        self.tool_router = tool_router
        self.remember_queue = remember_queue
        self.event_bus = event_bus
        self.session_lock = session_lock
        self.pause_waiter = pause_waiter
        self.step_context_assembler = step_context_assembler
        self.tool_output_store = tool_output_store
        self._task_queue: Any = None
        # Track active child sessions per parent session for /stop cancellation
        self._active_children: dict[str, dict[str, asyncio.Task[Any]]] = {}
        self._children_lock = asyncio.Lock()

    def set_task_queue(self, task_queue: Any) -> None:
        """Wire the task queue after construction (breaks circular dependency).

        Must be called before the first agent turn so that controller tools
        ``create_task`` and ``cancel_task`` can submit/cancel via the queue.
        """
        self._task_queue = task_queue

    async def run_step(
        self,
        ctx: StepContext,
        *,
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> StepOutput | None:
        """Run a single step as a full agentic loop.

        For Direct workflow (main chat): step_complete is optional.
        For multi-step workflows: step_complete is required.

        Returns StepOutput if the step completed, None if it failed.
        """
        start_time = datetime.now(UTC)
        logger.info(
            "agent: step started",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "step": ctx.step_definition.name,
                    "is_direct": ctx.is_direct,
                }
            },
        )
        await self.session_lock.acquire(ctx.session.session_id)
        try:
            return await self._execute_step(
                ctx, on_token=on_token, on_tool_call=on_tool_call, on_tool_result=on_tool_result
            )
        except StepInterrupted:
            raise
        except Exception as exc:
            logger.exception(
                "Agent loop step failed",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="error").inc()
            # Return a StepOutput with the error so it can be stored and
            # displayed in the UI instead of silently returning None.
            error_msg = f"{type(exc).__name__}: {exc}"
            return StepOutput(
                summary=f"Step failed: {type(exc).__name__}",
                error=error_msg[:2000],
            )
        finally:
            self.session_lock.release(ctx.session.session_id)
            duration = (datetime.now(UTC) - start_time).total_seconds()
            STEP_DURATION.labels(phase="total").observe(duration)

    async def _resolve_child_agent(
        self, child_agent_id: str, parent_agent: AgentDefinition
    ) -> AgentDefinition:
        """Resolve the AgentDefinition for a child session (#3).

        Falls back to the parent agent if lookup fails.
        """
        if child_agent_id == parent_agent.agent_id:
            return parent_agent
        try:
            from cognis.api.serializers import agent_to_response
            from cognis.store.queries import get_agent

            async with self.session_manager.session_factory() as db:
                agent_row = await get_agent(db, child_agent_id)
            if agent_row is not None:
                return AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
        except Exception:
            logger.warning(
                "delegation: failed to resolve child agent, using parent",
                extra={"extra_data": {"child_agent_id": child_agent_id}},
            )
        return parent_agent

    # ------------------------------------------------------------------
    # Child session tracking (for /stop cancellation)
    # ------------------------------------------------------------------

    async def _track_child(
        self, parent_session_id: str, child_session_id: str, task: asyncio.Task[Any]
    ) -> None:
        """Register an active child session task."""
        async with self._children_lock:
            self._active_children.setdefault(parent_session_id, {})[child_session_id] = task

    async def _untrack_child(self, parent_session_id: str, child_session_id: str) -> None:
        """Remove a child session from tracking."""
        async with self._children_lock:
            children = self._active_children.get(parent_session_id)
            if children:
                children.pop(child_session_id, None)
                if not children:
                    self._active_children.pop(parent_session_id, None)

    async def cancel_children(self, parent_session_id: str) -> int:
        """Cancel all active child sessions for a parent. Returns count cancelled."""
        async with self._children_lock:
            children = self._active_children.pop(parent_session_id, {})
        cancelled = 0
        for _child_id, task in children.items():
            if not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    # ------------------------------------------------------------------
    # Child session execution
    # ------------------------------------------------------------------

    async def _run_child_session(
        self,
        *,
        child_session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        task_description: str,
        parent_intaris_session_id: str,
        tool_registry: Any,
        executor_connection: Any,
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> StepOutput | None:
        """Run a delegated child session.

        Executes a single agent-loop turn on the child session using the
        task description as the user message.  The child session requires
        explicit ``step_complete`` (is_direct=False) to prevent premature
        completion.

        Returns the StepOutput on success, None on failure.

        Side effects (always executed):
        - Updates child session status in Cognis DB
        - Records delegation result in parent Intaris session
        - Publishes event bus events for frontend
        """
        conversation_id = conversation.conversation_id
        child_session_id = child_session.session_id

        # Resolve the correct agent if the child uses a different one
        resolved_agent = await self._resolve_child_agent(child_session.agent_id, agent)

        child_step = StepDefinition(name="delegation", type="run", prompt=task_description)
        child_ctx = StepContext(
            step_definition=child_step,
            session=child_session,
            conversation=conversation,
            agent=resolved_agent,
            is_direct=False,  # Require step_complete for sub-sessions
            user_message=task_description,
            system_initiated=True,
            interaction_mode="explicit_gates",
            tool_registry=tool_registry,
            executor_connection=executor_connection,
            orchestration_mode=OrchestrationMode.NONE,  # Sub-sessions cannot delegate
        )

        output: StepOutput | None = None

        # Set runtime context for JWT headers
        with scoped_runtime_context(
            user_email=child_session.user_email,
            agent_id=resolved_agent.agent_id,
        ):
            try:
                output = await self.run_step(
                    child_ctx,
                    on_token=on_token,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                result_summary = output.summary if output and output.summary else "Completed."
                result_content = output.content if output and output.content else ""

                # Update child session status — guarded
                try:
                    async with self.session_manager.session_factory() as db:
                        from cognis.store.queries import set_session_status

                        await set_session_status(
                            db,
                            child_session_id,
                            "completed",
                            completed_at=datetime.now(UTC),
                            result_summary=result_summary,
                        )
                        await db.commit()
                except Exception:
                    logger.warning(
                        "delegation: failed to update child session status",
                        extra={"extra_data": {"child_session_id": child_session_id}},
                        exc_info=True,
                    )

                # Record result in parent Intaris session — guarded
                try:
                    await self.providers.guardrails.record_events(
                        session_id=parent_intaris_session_id,
                        events=[
                            SessionEvent(
                                type="delegation",
                                data={
                                    "status": "completed",
                                    "child_session_id": child_session_id,
                                    "mode": "delegate",
                                    "result_summary": result_summary,
                                    "result_content": result_content,
                                },
                            )
                        ],
                        idempotency_key=(
                            f"{parent_intaris_session_id}:delegation_completed_{child_session_id}"
                        ),
                    )
                except Exception:
                    logger.warning(
                        "delegation: failed to record completion in parent session",
                        extra={"extra_data": {"child_session_id": child_session_id}},
                        exc_info=True,
                    )

                # Publish event bus event for frontend
                await self.event_bus.publish(
                    Event(
                        type=EventType.DELEGATION_COMPLETED,
                        data={
                            "conversation_id": conversation_id,
                            "child_session_id": child_session_id,
                            "parent_session_id": parent_intaris_session_id,
                            "result_summary": result_summary,
                        },
                    )
                )
                DELEGATIONS_TOTAL.labels(status="completed").inc()
                logger.info(
                    "delegation: child session completed",
                    extra={
                        "extra_data": {
                            "child_session_id": child_session_id,
                            "parent_session_id": parent_intaris_session_id,
                        }
                    },
                )
            except Exception:
                logger.exception(
                    "delegation: child session failed",
                    extra={
                        "extra_data": {
                            "child_session_id": child_session_id,
                            "parent_session_id": parent_intaris_session_id,
                        }
                    },
                )
                # Each operation guarded independently
                try:
                    async with self.session_manager.session_factory() as db:
                        from cognis.store.queries import set_session_status

                        await set_session_status(
                            db,
                            child_session_id,
                            "failed",
                            completed_at=datetime.now(UTC),
                            result_summary="Delegation failed",
                        )
                        await db.commit()
                except Exception:
                    logger.warning(
                        "delegation: failed to mark child session as failed", exc_info=True
                    )

                try:
                    await self.providers.guardrails.record_events(
                        session_id=parent_intaris_session_id,
                        events=[
                            SessionEvent(
                                type="delegation",
                                data={
                                    "status": "failed",
                                    "child_session_id": child_session_id,
                                    "mode": "delegate",
                                    "error": "Delegation execution failed",
                                },
                            )
                        ],
                        idempotency_key=(
                            f"{parent_intaris_session_id}:delegation_failed_{child_session_id}"
                        ),
                    )
                except Exception:
                    logger.warning(
                        "delegation: failed to record failure in parent session",
                        exc_info=True,
                    )

                # Publish event bus event for frontend
                await self.event_bus.publish(
                    Event(
                        type=EventType.DELEGATION_FAILED,
                        data={
                            "conversation_id": conversation_id,
                            "child_session_id": child_session_id,
                            "parent_session_id": parent_intaris_session_id,
                            "reason": "Delegation execution failed",
                        },
                    )
                )
                DELEGATIONS_TOTAL.labels(status="failed").inc()

        return output

    async def _run_child_session_async(
        self,
        *,
        child_session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        task_description: str,
        parent_intaris_session_id: str,
        tool_registry: Any,
        executor_connection: Any,
    ) -> None:
        """Async wrapper for _run_child_session that triggers follow-up turns.

        Used for wait=false (background) delegations.  After the child
        completes, publishes FOLLOW_UP_TURN_REQUESTED so the parent
        conversation gets a new system-initiated turn with the result.
        """
        parent_session_id = child_session.parent_session_id or ""
        child_session_id = child_session.session_id
        conversation_id = conversation.conversation_id

        try:
            output = await self._run_child_session(
                child_session=child_session,
                conversation=conversation,
                agent=agent,
                task_description=task_description,
                parent_intaris_session_id=parent_intaris_session_id,
                tool_registry=tool_registry,
                executor_connection=executor_connection,
            )
            status = "completed" if output else "failed"
        except Exception:
            status = "failed"
        finally:
            await self._untrack_child(parent_session_id, child_session_id)

        # Trigger a follow-up turn in the parent conversation
        await self.event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": conversation_id,
                    "status": status,
                },
            )
        )

    async def _execute_step(
        self,
        ctx: StepContext,
        *,
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> StepOutput | None:
        """Core step execution loop."""
        max_tool_calls = DEFAULT_MAX_TOOL_CALLS
        if ctx.agent.execution:
            max_tool_calls = ctx.agent.execution.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)

        tool_call_count = 0
        todo_reprompt_count = 0
        step_output: StepOutput | None = None
        events_to_record: list[SessionEvent] = []
        messages: list[dict[str, Any]] = []
        assistant_content_parts: list[str] = []  # Accumulate full assistant output

        # Build tool definitions for LLM (controller-injected tools)
        controller_tool_schemas = self._build_controller_tool_schemas(ctx)

        # Record user message to Intaris event store BEFORE context
        # assembly so the IntentionBarrier can start updating the session
        # intention while we assemble context and call the LLM.
        _user_msg_recorded_early = False
        if ctx.is_direct and ctx.user_message and not ctx.system_initiated:
            intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
            user_msg_event = SessionEvent(type="user_message", data={"content": ctx.user_message})
            try:
                await self.providers.guardrails.record_events(
                    session_id=intaris_id,
                    events=[user_msg_event],
                    source="cognis",
                )
                _user_msg_recorded_early = True
            except Exception:
                logger.warning(
                    "agent: failed to record early user_message event",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )

            # Trigger intention update. When the event was recorded
            # successfully, use from_events to avoid re-sending content.
            # Fall back to sending content directly if recording failed.
            try:
                if _user_msg_recorded_early:
                    await self.providers.guardrails.report_reasoning(
                        session_id=intaris_id,
                        from_events=True,
                    )
                else:
                    await self.providers.guardrails.report_reasoning(
                        session_id=intaris_id,
                        content=f"User message: {ctx.user_message}",
                    )
            except Exception:
                logger.warning(
                    "agent: failed to trigger intention update",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                    exc_info=True,
                )

        # Assemble initial context
        if not ctx.is_direct and not ctx.is_retry and self.step_context_assembler is not None:
            # First-attempt workflow step → use StepContextAssembler
            step_prompt = self._build_step_prompt(ctx)
            context_result = await self.step_context_assembler.assemble(
                session=ctx.session,
                conversation=ctx.conversation,
                agent=ctx.agent,
                step_definition=ctx.step_definition,
                step_index=ctx.step_index,
                workflow_steps=ctx.workflow_steps or [],
                workflow_state=ctx.workflow_state or WorkflowState(),
                step_prompt=step_prompt,
            )
            messages = context_result.messages
            events_to_record.append(
                SessionEvent(type="user_message", data={"content": step_prompt})
            )
        elif not ctx.is_direct and ctx.is_retry:
            # Retry → use regular assembler on existing session (which has
            # original prompt, prior work, and evaluation feedback already).
            # If state has fallback feedback (Intaris write failed), include it.
            feedback_text = ""
            if ctx.workflow_state is not None and ctx.workflow_state.last_evaluation_feedback:
                feedback_text = (
                    f"\n\n<evaluation_feedback>\n"
                    f"{ctx.workflow_state.last_evaluation_feedback}\n"
                    f"</evaluation_feedback>\n\n"
                )
                ctx.workflow_state.last_evaluation_feedback = None
            retry_message = (
                f"{feedback_text}Address the evaluation feedback above and complete this step."
            )
            context_result = await self.context_assembler.assemble(
                session=ctx.session,
                conversation=ctx.conversation,
                agent=ctx.agent,
                user_message=retry_message,
                user_message_role="user",
                tool_definitions=None,
                active_delegations=None,
            )
            messages = context_result.messages
            events_to_record.append(
                SessionEvent(type="user_message", data={"content": retry_message})
            )
        else:
            # Direct chat or fallback
            context_result = await self.context_assembler.assemble(
                session=ctx.session,
                conversation=ctx.conversation,
                agent=ctx.agent,
                user_message=ctx.user_message or ctx.step_definition.prompt,
                user_message_role="system" if ctx.system_initiated else "user",
                tool_definitions=None,
                active_delegations=None,
            )
            messages = context_result.messages

        # Record user message event for direct workflow (skip if already
        # recorded early for intention tracking above).
        if (
            ctx.is_direct
            and ctx.user_message
            and not ctx.system_initiated
            and not _user_msg_recorded_early
        ):
            events_to_record.append(
                SessionEvent(type="user_message", data={"content": ctx.user_message})
            )

        # Capture cache breakpoint for prompt caching (Anthropic cache_control)
        cache_breakpoint = getattr(context_result, "cache_breakpoint_index", None)

        # Store context usage in session cache for UI display and /context command
        self.session_cache.update_context_usage(
            ctx.session,
            prompt_tokens=context_result.prompt_tokens,
            max_context_tokens=context_result.max_context_tokens,
            model=context_result.resolved_model,
        )

        # Main agentic loop
        reprompted = False
        while True:
            self._raise_if_cancelled(ctx)

            # Prune old tool outputs before each LLM call to keep the
            # context window lean.  Uses tiktoken via the LLM provider for
            # accurate token estimation.
            resolved_model = getattr(context_result, "resolved_model", "")
            messages = prune_tool_outputs(
                messages,
                token_counter=lambda text, _m=resolved_model: self.providers.llm.count_tokens(
                    text, _m
                ),
            )

            # Resolve model and reasoning effort for this turn.
            # Chain: session override → agent config → system default.
            model_for_llm = self.session_cache.get_model_override(ctx.session.session_id) or (
                ctx.agent.llm_config.model if ctx.agent.llm_config else None
            )

            reasoning_effort = self.session_cache.get_reasoning_effort_override(
                ctx.session.session_id
            ) or (ctx.agent.llm_config.reasoning_effort if ctx.agent.llm_config else None)

            llm_kwargs: dict[str, Any] = {}
            if reasoning_effort:
                llm_kwargs["reasoning_effort"] = reasoning_effort

            # Stream LLM response
            accumulator = StreamAccumulator()
            async for chunk in self.providers.llm.stream_generate(
                messages,
                model=model_for_llm,
                task_type="default",
                tools=controller_tool_schemas + self._get_executor_tool_schemas(ctx),
                cache_breakpoint_index=cache_breakpoint,
                **llm_kwargs,
            ):
                text_delta = accumulator.feed(chunk)
                if text_delta and on_token:
                    await on_token(text_delta)

            content = accumulator.get_content()
            tool_calls = accumulator.get_tool_calls()

            # Record assistant message
            if content:
                events_to_record.append(
                    SessionEvent(type="assistant_message", data={"content": content})
                )
                messages.append({"role": "assistant", "content": content})
                assistant_content_parts.append(content)

            # No tool calls — check if step is complete
            if not tool_calls:
                if ctx.is_direct:
                    # Direct workflow (main chat): check for incomplete todos
                    incomplete_todos = self._get_incomplete_todos(ctx)
                    if incomplete_todos and todo_reprompt_count < _MAX_TODO_REPROMPTS:
                        todo_reprompt_count += 1
                        STEP_REPROMPTS.inc()
                        todo_list = "\n".join(
                            f"  - [{t.get('status', 'pending')}] {t.get('content', '')}"
                            for t in incomplete_todos
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"You have {len(incomplete_todos)} incomplete todos:\n"
                                    f"{todo_list}\n\n"
                                    "Continue working on them, delegate to sub-sessions "
                                    "or tasks for longer work, or cancel remaining todos "
                                    "via step_todo_write if they are no longer needed."
                                ),
                            }
                        )
                        continue
                    # Todos done (or max re-prompts reached) — complete
                    step_output = StepOutput(
                        summary=content[:500] if content else "",
                        content="\n\n".join(assistant_content_parts),
                        outputs={},
                        claims=[],
                    )
                    break
                elif not reprompted:
                    # Non-direct (sub-session / workflow step): require step_complete
                    STEP_REPROMPTS.inc()
                    reprompted = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You must call step_complete to finish this step. "
                                "Please summarize what you have accomplished and call step_complete."
                            ),
                        }
                    )
                    continue
                else:
                    # Failed to call step_complete after re-prompt
                    step_output = None
                    break

            # Process tool calls
            if tool_calls and content:
                # Add assistant message with tool calls for chat history
                messages[-1] = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in tool_calls
                    ],
                }
            elif tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tc.call_id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )

            delegation_spawned = False
            for tc in tool_calls:
                self._raise_if_cancelled(ctx)
                tool_call_count += 1
                STEP_TOOL_CALLS.labels(tool_name=tc.name).inc()

                if on_tool_call:
                    await on_tool_call(tc.name, tc.call_id, tc.arguments)

                # Controller tool interception
                if tc.name == STEP_COMPLETE:
                    _append_tool_call_event(events_to_record, tc)
                    # Reject step_complete in direct mode (main chat)
                    if ctx.is_direct:
                        err_content = json.dumps(
                            {
                                "status": "error",
                                "message": (
                                    "step_complete is not available in this context. "
                                    "Simply respond to the user directly."
                                ),
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(events_to_record, tc, err_content, True)
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    # Enforce todo completion for non-direct steps
                    incomplete_todos = self._get_incomplete_todos(ctx)
                    if incomplete_todos and not ctx.is_direct:
                        todo_list = ", ".join(t.get("content", "?") for t in incomplete_todos[:5])
                        err_content = json.dumps(
                            {
                                "status": "rejected",
                                "reason": "incomplete_todos",
                                "message": (
                                    f"Cannot complete: {len(incomplete_todos)} todos still "
                                    f"pending ({todo_list}). Complete or cancel them first "
                                    "via step_todo_write."
                                ),
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(events_to_record, tc, err_content, True)
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    step_output = StepOutput(
                        summary=tc.arguments.get("summary", ""),
                        content="\n\n".join(assistant_content_parts),
                        outputs=tc.arguments.get("outputs", {}),
                        claims=tc.arguments.get("claims", []),
                    )
                    events_to_record.append(
                        SessionEvent(
                            type="lifecycle",
                            data={
                                "event": "step_complete",
                                "status": "completed",
                                "summary": step_output.summary,
                            },
                        )
                    )
                    result_content = json.dumps({"status": "completed"})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(events_to_record, tc, result_content, False)
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    break

                elif tc.name == STEP_TODO_WRITE:
                    _append_tool_call_event(events_to_record, tc)
                    ctx.todos = tc.arguments.get("todos", [])
                    result_content = json.dumps({"status": "updated", "count": len(ctx.todos)})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(events_to_record, tc, result_content, False)
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == STEP_TODO_LIST:
                    _append_tool_call_event(events_to_record, tc)
                    result_content = json.dumps({"todos": ctx.todos})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(events_to_record, tc, result_content, False)
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == STEP_REQUEST_INPUT:
                    _append_tool_call_event(events_to_record, tc)
                    if (
                        ctx.interaction_mode != "step_requests"
                        or not ctx.step_definition.allow_questions
                    ):
                        err_content = json.dumps(
                            {"error": "Input requests are not enabled for this step."}
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(events_to_record, tc, err_content, True)
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    recovered_response = self._get_recovered_step_response(ctx)
                    if recovered_response is not None:
                        await self._clear_interactive_pause_state(ctx)
                        rec_content = json.dumps({"response": recovered_response})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": rec_content}
                        )
                        _append_tool_result_event(events_to_record, tc, rec_content, False)
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, rec_content, False, None, None
                            )
                        continue

                    # Pause and wait for input
                    pause_id = f"input_{uuid.uuid4().hex[:12]}"
                    question = tc.arguments.get("question", "")
                    options = tc.arguments.get("options")
                    pause_context = tc.arguments.get("context")
                    pause_options = (
                        [str(option) for option in options] if isinstance(options, list) else None
                    )
                    self.pause_waiter.register(
                        PendingPause(
                            pause_id=pause_id,
                            pause_type="step_input",
                            task_id=ctx.task_id,
                            step_name=ctx.step_definition.name,
                            step_run_id=ctx.step_run_id,
                            session_id=ctx.session.session_id,
                            question=question,
                            options=(
                                [{"label": option, "action": option} for option in pause_options]
                                if pause_options is not None
                                else None
                            ),
                            context={"context": pause_context}
                            if isinstance(pause_context, str)
                            else None,
                        )
                    )
                    await self._set_interactive_pause_state(
                        ctx,
                        pause_type="step_input",
                        pause_payload={
                            "pause_id": pause_id,
                            "step_name": ctx.step_definition.name,
                            "step_run_id": ctx.step_run_id,
                            "session_id": ctx.session.session_id,
                            "question": question,
                            "options": pause_options,
                            "context": pause_context,
                        },
                    )
                    await self.event_bus.publish(
                        Event(
                            type=EventType.STEP_PAUSED,
                            data={
                                "pause_id": pause_id,
                                "pause_type": "step_input",
                                "question": question,
                                "options": pause_options,
                                "context": pause_context,
                                "session_id": ctx.session.session_id,
                                "task_id": ctx.task_id,
                                "step_name": ctx.step_definition.name,
                                "step_run_id": ctx.step_run_id,
                            },
                        )
                    )
                    try:
                        resolution = await self.pause_waiter.wait(pause_id, timeout=300.0)
                        await self._clear_interactive_pause_state(ctx)
                        if resolution.decision == "cancel":
                            # No tool_result event needed: StepInterrupted propagates
                            # without calling _finalize_step, so events_to_record is discarded.
                            raise StepInterrupted("Step input request cancelled")
                        resp_content = json.dumps({"response": resolution.data.get("response", "")})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": resp_content}
                        )
                        _append_tool_result_event(events_to_record, tc, resp_content, False)
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, resp_content, False, None, None
                            )
                    except TimeoutError:
                        await self._clear_interactive_pause_state(ctx)
                        timeout_content = json.dumps({"error": "Input request timed out."})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": timeout_content}
                        )
                        _append_tool_result_event(events_to_record, tc, timeout_content, True)
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, timeout_content, True, None, None
                            )
                    continue

                elif is_orchestration_tool(tc.name):
                    # Orchestration tool — intercept as controller directive
                    _append_tool_call_event(events_to_record, tc)
                    orch_result = await self._handle_orchestration_tool(
                        tc,
                        ctx=ctx,
                        events_to_record=events_to_record,
                        on_token=on_token,
                        on_tool_call=on_tool_call,
                        on_tool_result=on_tool_result,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": orch_result.output,
                        }
                    )
                    _append_tool_result_event(
                        events_to_record, tc, orch_result.output, orch_result.is_error
                    )
                    if on_tool_result:
                        await on_tool_result(
                            tc.call_id,
                            tc.name,
                            orch_result.output,
                            orch_result.is_error,
                            None,
                            None,
                        )
                    # Check if an async delegation was spawned
                    if orch_result.metadata and orch_result.metadata.get("delegation_spawned"):
                        delegation_spawned = True
                    continue

                else:
                    # Regular tool call — route through tool router
                    await self.event_bus.publish(
                        Event(
                            type=EventType.WORKFLOW_PROGRESS,
                            data={
                                "event": "tool_call_started",
                                "task_id": ctx.task_id,
                                "session_id": ctx.session.session_id,
                                "step_name": ctx.step_definition.name,
                                "step_run_id": ctx.step_run_id,
                                "call_id": tc.call_id,
                                "tool_name": tc.name,
                            },
                        )
                    )
                    events_to_record.append(
                        SessionEvent(
                            type="tool_call",
                            data={
                                "name": tc.name,
                                "call_id": tc.call_id,
                                "arguments": _truncate_tool_data(
                                    json.dumps(tc.arguments, default=str)
                                ),
                            },
                        )
                    )

                    result = await self.tool_router.execute(
                        tc,
                        ctx.session,
                        ctx.agent,
                        self._get_tool_registry(ctx),
                        self._get_executor(ctx),
                    )

                    # -------------------------------------------------------
                    # Escalation blocking: pause and wait for user approval
                    # -------------------------------------------------------
                    result = await self._handle_escalation(
                        result, tc, ctx, events_to_record, on_tool_result
                    )

                    # Save full output to the tool output store for later
                    # exploration via read_tool_output / search_tool_output.
                    # The raw output (before XML wrapping) is what we store.
                    raw_output = result.metadata.get("_raw_output") if result.metadata else None
                    if raw_output and self.tool_output_store is not None:
                        await self.tool_output_store.save(tc.call_id, raw_output)

                    # Intaris gets a middle-truncated preview (larger than
                    # the WS preview) so compaction and audit have useful
                    # context without bloating the event stream.
                    intaris_preview, _ = middle_truncate(
                        result.output, _MAX_INTARIS_TOOL_RESULT, call_id=tc.call_id
                    )
                    original_size = (
                        result.metadata.get("original_size") if result.metadata else None
                    )
                    events_to_record.append(
                        SessionEvent(
                            type="tool_result",
                            data={
                                "call_id": tc.call_id,
                                "name": tc.name,
                                "is_error": result.is_error,
                                "duration_ms": result.duration_ms,
                                "result": intaris_preview,
                                "output_size": original_size or len(result.output),
                                "has_full_output": raw_output is not None,
                            },
                        )
                    )
                    ws_preview = _truncate_tool_data(result.output)
                    eval_meta = result.metadata.get("evaluation") if result.metadata else None
                    if on_tool_result:
                        await on_tool_result(
                            tc.call_id,
                            tc.name,
                            ws_preview,
                            result.is_error,
                            result.duration_ms,
                            eval_meta,
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": result.output,
                        }
                    )
                    await self.event_bus.publish(
                        Event(
                            type=EventType.WORKFLOW_PROGRESS,
                            data={
                                "event": "tool_call_completed",
                                "task_id": ctx.task_id,
                                "session_id": ctx.session.session_id,
                                "step_name": ctx.step_definition.name,
                                "step_run_id": ctx.step_run_id,
                                "call_id": tc.call_id,
                                "tool_name": tc.name,
                                "is_error": result.is_error,
                            },
                        )
                    )

            # Flush events incrementally for workflow steps so they are
            # visible in session logs during execution (direct chat uses
            # WebSocket streaming instead).
            if not ctx.is_direct and events_to_record:
                await self._flush_events_incremental(ctx, events_to_record)

            # Check if step_complete was called in this batch
            if step_output is not None:
                break

            # Delegation spawned — end the parent turn after processing
            # the full tool batch.  The child runs in the background and
            # a follow-up turn will be triggered on completion.
            if delegation_spawned:
                step_output = StepOutput(
                    summary="Delegation spawned — working in background.",
                    content="\n\n".join(assistant_content_parts),
                )
                break

            # Check tool call limit
            if tool_call_count >= max_tool_calls:
                logger.warning(
                    "Tool call limit reached",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "count": tool_call_count,
                        }
                    },
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Tool call limit ({max_tool_calls}) reached. "
                            "Please call step_complete to finish this step."
                        ),
                    }
                )

        # Finalize step — pass assistant_content_parts so Mnemory remember
        # works even when events were already flushed incrementally.
        events_recorded = await self._finalize_step(
            ctx, events_to_record, assistant_content_parts=assistant_content_parts
        )

        # Automatic compaction: if context assembly recommended compaction
        # and events were successfully recorded, compact + rotate session
        # so the next turn starts with a clean context window.  Only for
        # direct chat — workflow steps have their own lifecycle management.
        if (
            events_recorded
            and ctx.is_direct
            and getattr(context_result, "recommend_compaction", False)
        ):
            await self._auto_compact(ctx)

        if step_output:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="completed").inc()
        else:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="failed").inc()

        return step_output

    # ------------------------------------------------------------------
    # Escalation blocking
    # ------------------------------------------------------------------

    async def _handle_escalation(
        self,
        result: ToolResult,
        tc: ToolCall,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        on_tool_result: ToolResultCallback | None,
    ) -> ToolResult:
        """If the tool was escalated, block and wait for user approval.

        On approval the tool is re-executed (Intaris auto-approves via its
        escalation retry cache).  On denial or timeout the escalation error
        is returned to the LLM.  Timeout does NOT submit a deny to Intaris
        so future similar calls are not prejudiced.
        """
        eval_meta = result.metadata.get("evaluation") if result.metadata else None
        if not eval_meta or eval_meta.get("decision") != "escalate":
            return result

        intaris_call_id = eval_meta.get("call_id")
        if not intaris_call_id:
            # No call_id from Intaris — cannot track, treat as denied
            return result

        pause_id = f"escalation:{intaris_call_id}"
        conversation_id = ctx.conversation.conversation_id

        # Read escalation timeout from settings
        async with self.session_manager.session_factory() as db:
            timeout_raw: int = await get_setting_value(  # type: ignore[assignment]
                db, "session.escalation_timeout_seconds", 300
            )
        timeout_f = float(timeout_raw)

        # Register the pause so WebSocket/REST handlers can find it
        self.pause_waiter.register(
            PendingPause(
                pause_id=pause_id,
                pause_type="escalation",
                session_id=ctx.session.session_id,
                conversation_id=conversation_id,
                context={
                    "call_id": intaris_call_id,
                    "tool_name": tc.name,
                    "arguments": tc.arguments,
                    "risk": eval_meta.get("risk"),
                    "reasoning": eval_meta.get("reasoning"),
                },
            )
        )

        # Notify all channel subscribers (web, signal, slack, etc.)
        await self.event_bus.publish(
            Event(
                type=EventType.ESCALATION_CREATED,
                data={
                    "conversation_id": conversation_id,
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "tool_name": tc.name,
                    "risk": eval_meta.get("risk"),
                    "reasoning": eval_meta.get("reasoning"),
                    "timeout_seconds": timeout_raw,
                },
            )
        )

        # Send an interim tool_result to the WebSocket so the UI shows
        # the escalation status on the tool call block immediately.
        if on_tool_result:
            await on_tool_result(
                tc.call_id,
                tc.name,
                "Waiting for user approval...",
                True,
                None,
                {**eval_meta, "pending": True},
            )

        logger.info(
            "Escalation: waiting for user decision",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "tool_name": tc.name,
                    "timeout_seconds": timeout_raw,
                }
            },
        )

        # Block until resolved or timeout
        try:
            resolution = await self.pause_waiter.wait(pause_id, timeout=timeout_f)
        except TimeoutError:
            resolution = PauseResolution(decision="deny", data={"reason": "timeout"})

        # Publish resolution event to all channel subscribers
        await self.event_bus.publish(
            Event(
                type=EventType.ESCALATION_RESOLVED,
                data={
                    "conversation_id": conversation_id,
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "decision": resolution.decision,
                    "reason": resolution.data.get("reason"),
                },
            )
        )

        if resolution.decision == "approve":
            logger.info(
                "Escalation approved — re-executing tool",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "call_id": intaris_call_id,
                        "tool_name": tc.name,
                    }
                },
            )
            # Re-execute: Intaris auto-approves via escalation retry (10 min)
            result = await self.tool_router.execute(
                tc,
                ctx.session,
                ctx.agent,
                self._get_tool_registry(ctx),
                self._get_executor(ctx),
            )
            # If Intaris still escalates on retry (shouldn't happen), treat
            # as denied to avoid infinite loops.
            retry_eval = result.metadata.get("evaluation") if result.metadata else None
            if retry_eval and retry_eval.get("decision") == "escalate":
                logger.warning(
                    "Escalation retry still escalated — treating as denied",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "tool_name": tc.name,
                        }
                    },
                )
                return ToolResult(
                    output="Tool denied: approval could not be verified.",
                    is_error=True,
                    metadata=result.metadata,
                )
            return result

        # Denied by user or timed out
        if resolution.data.get("reason") == "timeout":
            reason = "Escalation timed out — no response received."
        else:
            note = resolution.data.get("note", "")
            reason = f"User denied the tool call. {note}".strip()

        logger.info(
            "Escalation denied",
            extra={
                "extra_data": {
                    "session_id": ctx.session.session_id,
                    "call_id": intaris_call_id,
                    "decision": resolution.decision,
                    "reason": resolution.data.get("reason"),
                }
            },
        )
        return ToolResult(
            output=reason,
            is_error=True,
            metadata=result.metadata,
        )

    # ------------------------------------------------------------------
    # Orchestration tool dispatch
    # ------------------------------------------------------------------

    async def _handle_orchestration_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> ToolResult:
        """Dispatch an orchestration tool call.

        Returns a ToolResult.  For async delegations the metadata includes
        ``delegation_spawned=True`` so the caller can end the parent turn.
        """
        # Check orchestration mode
        if ctx.orchestration_mode == OrchestrationMode.NONE:
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": (
                            "Orchestration tools are not available in sub-sessions. "
                            "Complete the task directly."
                        ),
                    }
                ),
                is_error=True,
            )

        # In DELEGATE_SYNC_ONLY mode, only delegate is allowed (and always sync)
        if ctx.orchestration_mode == OrchestrationMode.DELEGATE_SYNC_ONLY and tc.name != "delegate":
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": (
                            f"Tool '{tc.name}' is not available in task steps. "
                            "Only 'delegate' (sync) is available."
                        ),
                    }
                ),
                is_error=True,
            )

        # Dispatch by tool name
        if tc.name == "delegate":
            return await self._handle_delegate(
                tc,
                ctx=ctx,
                events_to_record=events_to_record,
                on_token=on_token,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
            )
        elif is_subsession_tool(tc.name):
            return await self._handle_subsession_management(tc, ctx=ctx)
        elif is_task_tool(tc.name):
            return await self._handle_task_tool(tc, ctx=ctx)
        else:
            return ToolResult(
                output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
                is_error=True,
            )

    async def _handle_delegate(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
        on_token: TokenCallback | None = None,
        on_tool_call: ToolCallCallback | None = None,
        on_tool_result: ToolResultCallback | None = None,
    ) -> ToolResult:
        """Handle the delegate tool — create and optionally run a child session."""
        task_description = tc.arguments.get("task", "")
        wait = tc.arguments.get("wait", False)

        # In DELEGATE_SYNC_ONLY mode (task steps), force sync
        if ctx.orchestration_mode == OrchestrationMode.DELEGATE_SYNC_ONLY:
            wait = True

        result, child_session = await handle_delegate_tool_call(
            tc,
            session_manager=self.session_manager,
            session=ctx.session,
            agent=ctx.agent,
        )

        if child_session is None:
            # Creation failed — record error event
            events_to_record.append(
                SessionEvent(
                    type="delegation",
                    data={
                        "mode": "delegate",
                        "call_id": tc.call_id,
                        "task": task_description,
                        "error": "Child session creation failed",
                    },
                )
            )
            return result

        parent_intaris_id = ctx.session.intaris_session_id or ctx.session.session_id

        # Record delegation started event
        events_to_record.append(
            SessionEvent(
                type="delegation",
                data={
                    "mode": "delegate",
                    "call_id": tc.call_id,
                    "task": task_description,
                    "child_session_id": child_session.session_id,
                    "wait": wait,
                },
            )
        )

        await self.event_bus.publish(
            Event(
                type=EventType.DELEGATION_STARTED,
                data={
                    "conversation_id": ctx.conversation.conversation_id,
                    "parent_session_id": ctx.session.session_id,
                    "child_session_id": child_session.session_id,
                    "mode": "delegate",
                    "agent_id": child_session.agent_id,
                    "task": task_description,
                    "wait": wait,
                },
            )
        )

        if wait:
            # Synchronous delegation — await inline, return output as tool result.
            # Do NOT pass parent streaming callbacks to child sessions — child
            # tool calls should only be visible in the sub-session view, not
            # leak into the parent conversation timeline.
            DELEGATIONS_TOTAL.labels(status="sync_started").inc()
            output = await self._run_child_session(
                child_session=child_session,
                conversation=ctx.conversation,
                agent=ctx.agent,
                task_description=task_description,
                parent_intaris_session_id=parent_intaris_id,
                tool_registry=ctx.tool_registry,
                executor_connection=ctx.executor_connection,
            )
            if output:
                # Prefer full content over summary for delegation results
                result_text = output.content if output.content else output.summary
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "completed",
                            "session_id": child_session.session_id,
                            "summary": output.summary,
                            "result": result_text,
                            "outputs": output.outputs,
                        },
                        default=str,
                    ),
                    metadata={"orchestration": True, "mode": "delegate", "wait": True},
                )
            else:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "failed",
                            "session_id": child_session.session_id,
                            "message": "Sub-session failed to complete.",
                        }
                    ),
                    is_error=True,
                    metadata={"orchestration": True, "mode": "delegate", "wait": True},
                )
        else:
            # Asynchronous delegation — spawn background task
            child_task = asyncio.create_task(
                self._run_child_session_async(
                    child_session=child_session,
                    conversation=ctx.conversation,
                    agent=ctx.agent,
                    task_description=task_description,
                    parent_intaris_session_id=parent_intaris_id,
                    tool_registry=ctx.tool_registry,
                    executor_connection=ctx.executor_connection,
                )
            )
            child_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            await self._track_child(ctx.session.session_id, child_session.session_id, child_task)
            DELEGATIONS_TOTAL.labels(status="spawned").inc()
            # Mark delegation_spawned in metadata so the caller ends the turn
            result_with_flag = ToolResult(
                output=result.output,
                metadata={
                    **(result.metadata or {}),
                    "delegation_spawned": True,
                },
            )
            return result_with_flag

    async def _handle_subsession_management(self, tc: ToolCall, *, ctx: StepContext) -> ToolResult:
        """Handle list_subsessions, get_subsession, cancel_subsession."""
        from cognis.store.queries import get_session_row, list_child_sessions, set_session_status

        parent_session_id = ctx.session.session_id

        if tc.name == "list_subsessions":
            status_filter = tc.arguments.get("status", "all")
            async with self.session_manager.session_factory() as db:
                children = await list_child_sessions(db, parent_session_id)
            items = []
            for child in children:
                if status_filter != "all" and child.status != status_filter:
                    continue
                items.append(
                    {
                        "session_id": child.session_id,
                        "agent_id": child.agent_id,
                        "status": child.status,
                        "task": child.delegation_task,
                        "result_summary": child.result_summary,
                        "started_at": str(child.started_at) if child.started_at else None,
                        "completed_at": str(child.completed_at) if child.completed_at else None,
                    }
                )
            return ToolResult(
                output=json.dumps({"subsessions": items, "count": len(items)}, default=str),
            )

        elif tc.name == "get_subsession":
            target_id = tc.arguments.get("session_id", "")
            async with self.session_manager.session_factory() as db:
                target_row = await get_session_row(db, target_id)
            if target_row is None or target_row.parent_session_id != parent_session_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Sub-session not found or not a child of this session.",
                        }
                    ),
                    is_error=True,
                )
            return ToolResult(
                output=json.dumps(
                    {
                        "session_id": target_row.session_id,
                        "agent_id": target_row.agent_id,
                        "status": target_row.status,
                        "task": target_row.delegation_task,
                        "result_summary": target_row.result_summary,
                        "started_at": str(target_row.started_at) if target_row.started_at else None,
                        "completed_at": (
                            str(target_row.completed_at) if target_row.completed_at else None
                        ),
                    },
                    default=str,
                ),
            )

        elif tc.name == "cancel_subsession":
            cancel_id = tc.arguments.get("session_id", "")
            # Verify it's a child of this session
            async with self.session_manager.session_factory() as db:
                cancel_row = await get_session_row(db, cancel_id)
            if cancel_row is None or cancel_row.parent_session_id != parent_session_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Sub-session not found or not a child of this session.",
                        }
                    ),
                    is_error=True,
                )
            if cancel_row.status != "active":
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": f"Sub-session is already {cancel_row.status}.",
                        }
                    ),
                    is_error=True,
                )
            # Cancel the asyncio task if tracked
            async with self._children_lock:
                active_children = self._active_children.get(parent_session_id, {})
                child_task = active_children.pop(cancel_id, None)
            if child_task and not child_task.done():
                child_task.cancel()
            # Mark as failed in DB
            async with self.session_manager.session_factory() as db:
                await set_session_status(
                    db,
                    cancel_id,
                    "failed",
                    completed_at=datetime.now(UTC),
                    result_summary="Cancelled by parent session",
                )
                await db.commit()
            return ToolResult(
                output=json.dumps({"status": "cancelled", "session_id": cancel_id}),
            )

        return ToolResult(
            output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
            is_error=True,
        )

    async def _handle_task_tool(self, tc: ToolCall, *, ctx: StepContext) -> ToolResult:
        """Handle create_task, list_tasks, get_task, update_task, cancel_task."""
        from cognis.store.queries import (
            get_task,
            list_tasks_for_agent,
            update_task_fields,
            update_task_status,
        )

        if tc.name == "create_task":
            task_queue = self._task_queue
            if task_queue is None:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Task queue is not available.",
                        }
                    ),
                    is_error=True,
                )
            try:
                from cognis.models.task import TaskDelivery

                task = await task_queue.submit(
                    created_by=ctx.session.user_email,
                    agent_id=tc.arguments.get("agent_id") or ctx.agent.agent_id,
                    title=tc.arguments.get("title", "Untitled task"),
                    description=tc.arguments.get("description", ""),
                    priority=tc.arguments.get("priority", 0),
                    source_type="agent",
                    source_ref=ctx.conversation.conversation_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    workflow_id=tc.arguments.get("workflow_id"),
                )
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "created",
                            "task_id": task.task_id,
                            "title": task.title,
                            "message": "Task created and queued for execution.",
                        }
                    ),
                )
            except Exception as exc:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": f"Failed to create task: {exc}",
                        }
                    ),
                    is_error=True,
                )

        elif tc.name == "list_tasks":
            status_filter = tc.arguments.get("status", "all")
            statuses = None if status_filter == "all" else [status_filter]
            async with self.session_manager.session_factory() as db:
                tasks = await list_tasks_for_agent(db, ctx.agent.agent_id, statuses=statuses)
            items = []
            for t in tasks:
                items.append(
                    {
                        "task_id": t.task_id,
                        "title": t.title,
                        "status": t.status,
                        "priority": t.priority,
                        "workflow_id": t.workflow_id,
                        "created_at": str(t.created_at) if t.created_at else None,
                        "result_summary": t.result_summary,
                    }
                )
            return ToolResult(
                output=json.dumps({"tasks": items, "count": len(items)}, default=str),
            )

        elif tc.name == "get_task":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
            if task_row is None:
                return ToolResult(
                    output=json.dumps({"status": "error", "message": "Task not found."}),
                    is_error=True,
                )
            # Verify agent access
            if task_row.agent_id != ctx.agent.agent_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Task belongs to a different agent.",
                        }
                    ),
                    is_error=True,
                )
            return ToolResult(
                output=json.dumps(
                    {
                        "task_id": task_row.task_id,
                        "title": task_row.title,
                        "description": task_row.description,
                        "status": task_row.status,
                        "priority": task_row.priority,
                        "workflow_id": task_row.workflow_id,
                        "created_at": str(task_row.created_at) if task_row.created_at else None,
                        "started_at": str(task_row.started_at) if task_row.started_at else None,
                        "completed_at": str(task_row.completed_at)
                        if task_row.completed_at
                        else None,
                        "result_summary": task_row.result_summary,
                    },
                    default=str,
                ),
            )

        elif tc.name == "update_task":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"status": "error", "message": "Task not found."}),
                        is_error=True,
                    )
                if task_row.agent_id != ctx.agent.agent_id:
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "message": "Task belongs to a different agent.",
                            }
                        ),
                        is_error=True,
                    )
                if task_row.status not in ("draft", "queued"):
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "error",
                                "message": (
                                    f"Cannot update task in '{task_row.status}' status. "
                                    "Only draft or queued tasks can be updated."
                                ),
                            }
                        ),
                        is_error=True,
                    )
                ok = await update_task_fields(
                    db,
                    task_id,
                    title=tc.arguments.get("title"),
                    description=tc.arguments.get("description"),
                    priority=tc.arguments.get("priority"),
                    workflow_id=tc.arguments.get("workflow_id"),
                )
                await db.commit()
            if ok:
                return ToolResult(
                    output=json.dumps({"status": "updated", "task_id": task_id}),
                )
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "error",
                        "message": "No fields to update or update failed.",
                    }
                ),
                is_error=True,
            )

        elif tc.name == "cancel_task":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
            if task_row is None:
                return ToolResult(
                    output=json.dumps({"status": "error", "message": "Task not found."}),
                    is_error=True,
                )
            if task_row.agent_id != ctx.agent.agent_id:
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": "Task belongs to a different agent.",
                        }
                    ),
                    is_error=True,
                )
            if task_row.status in ("completed", "failed", "cancelled"):
                return ToolResult(
                    output=json.dumps(
                        {
                            "status": "error",
                            "message": f"Task is already {task_row.status}.",
                        }
                    ),
                    is_error=True,
                )
            # Use task queue cancel if available, otherwise direct DB update
            task_queue = self._task_queue
            if task_queue is not None:
                with contextlib.suppress(Exception):
                    await task_queue.cancel_task(task_id)
            else:
                async with self.session_manager.session_factory() as db:
                    await update_task_status(
                        db,
                        task_id,
                        "cancelled",
                        completed_at=datetime.now(UTC),
                    )
                    await db.commit()
            return ToolResult(
                output=json.dumps({"status": "cancelled", "task_id": task_id}),
            )

        return ToolResult(
            output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
            is_error=True,
        )

    @staticmethod
    def _get_incomplete_todos(ctx: StepContext) -> list[dict[str, Any]]:
        """Return todos that are not done or cancelled."""
        return [t for t in ctx.todos if t.get("status") not in ("done", "cancelled")]

    async def _flush_events_incremental(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
    ) -> None:
        """Flush accumulated events to Intaris without finalizing the step.

        Used for workflow steps to make events visible in session logs
        during execution instead of waiting for the entire step to complete.
        Events are moved out of the list (cleared) on success so they are
        not recorded again by ``_finalize_step``.
        """
        if not events:
            return
        batch = list(events)
        intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
        try:
            append_result = await self.providers.guardrails.record_events(
                session_id=intaris_id,
                events=batch,
                source="cognis",
            )
            if append_result.ok:
                await self.session_cache.append_recorded_events(ctx.session, batch, append_result)
                events.clear()
            else:
                logger.debug(
                    "agent: incremental flush returned ok=False, will retry at finalize",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
        except Exception:
            # Non-fatal — events stay in the list and will be retried
            # by _finalize_step at the end of the step.
            logger.debug(
                "agent: incremental flush failed, will retry at finalize",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
                exc_info=True,
            )

    async def _finalize_step(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        assistant_content_parts: list[str] | None = None,
    ) -> bool:
        """Record events to Intaris, update cache, dispatch remember.

        Returns ``True`` if events were successfully recorded to Intaris,
        ``False`` otherwise.  Callers use this to gate post-turn operations
        like automatic compaction — running compaction after a failed write
        would lose the current turn's events.
        """
        if not events:
            # Events may have been flushed incrementally — still dispatch
            # Mnemory remember using the accumulated assistant content.
            await self._dispatch_remember(ctx, assistant_content_parts)
            return True

        intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
        idempotency_key = f"{intaris_id}:step_{uuid.uuid4().hex[:8]}"

        events_recorded = False
        try:
            append_result = await self.providers.guardrails.record_events(
                session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                events=events,
                source="cognis",
                idempotency_key=idempotency_key,
            )
            if append_result.ok:
                # Update session cache with recorded events
                await self.session_cache.append_recorded_events(ctx.session, events, append_result)
                events_recorded = True
                logger.info(
                    "agent: events recorded",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "event_count": len(events),
                            "last_seq": append_result.last_seq,
                        }
                    },
                )
            else:
                logger.warning(
                    "agent: record_events returned ok=False, events not persisted",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "event_count": len(events),
                        }
                    },
                )
        except Exception:
            logger.exception(
                "agent: failed to record events to Intaris",
                extra={
                    "extra_data": {"session_id": ctx.session.session_id, "event_count": len(events)}
                },
            )

        # Dispatch remember — use assistant_content_parts if provided
        # (covers incrementally-flushed events), fall back to extracting
        # from the events list (covers the non-incremental path).
        if assistant_content_parts is None:
            extracted = [
                e.data.get("content", "")
                for e in events
                if e.type == "assistant_message" and e.data.get("content")
            ]
            await self._dispatch_remember(ctx, extracted or None)
        else:
            await self._dispatch_remember(ctx, assistant_content_parts)

        return events_recorded

    async def _dispatch_remember(
        self,
        ctx: StepContext,
        content_parts: list[str] | None,
    ) -> None:
        """Enqueue assistant content to Mnemory remember queue."""
        if not content_parts:
            return
        if not ctx.session.mnemory_session_id:
            logger.warning(
                "agent: skipping remember — no mnemory_session_id",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            return
        assistant_content = " ".join(content_parts)
        if assistant_content.strip():
            await self.remember_queue.enqueue(
                {
                    "session_id": ctx.session.mnemory_session_id,
                    "messages": [{"role": "assistant", "content": assistant_content[:5000]}],
                    "user_email": ctx.session.user_email,
                    "agent_id": ctx.session.agent_id,
                }
            )

    async def _auto_compact(self, ctx: StepContext) -> None:
        """Automatically compact and rotate the session post-turn.

        Called when ``context_result.recommend_compaction`` was ``True``
        and events were successfully recorded.  Runs LLM compaction →
        session rotation → cache pre-population.  On failure, the turn
        has already completed successfully so we log and continue —
        the next turn will re-trigger compaction.

        Bounded to ``AUTO_COMPACTION_TIMEOUT_SECONDS`` to avoid holding
        the session lock indefinitely under provider degradation.
        """

        # Early exit: skip if session cache has very few events
        # (e.g. manual /compact just ran and deferred creation already
        # created a near-empty session).
        cache_entry = self.session_cache.get_entry(ctx.session.session_id)
        if cache_entry is not None:
            preserve_turns = getattr(self.compaction_strategy, "preserve_turns", 10)
            user_event_count = sum(1 for e in cache_entry.events if e.type == "user_message")
            if user_event_count <= preserve_turns:
                logger.debug(
                    "agent: auto-compact skipped — too few events",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "user_events": user_event_count,
                            "preserve_turns": preserve_turns,
                        }
                    },
                )
                return

        logger.info(
            "agent: auto-compaction triggered",
            extra={"extra_data": {"session_id": ctx.session.session_id}},
        )

        with AUTO_COMPACTION_DURATION.time():
            try:
                compaction_result = await asyncio.wait_for(
                    self.compaction_strategy.compact(ctx.session, trigger="automatic"),
                    timeout=AUTO_COMPACTION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "agent: auto-compaction timed out, skipping — will retry next turn",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
                return
            except Exception:
                logger.warning(
                    "agent: auto-compaction failed, skipping — will retry next turn",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                    exc_info=True,
                )
                return

            if not compaction_result.compacted:
                return

            # Rotate session: create new root session with clean Intaris stream
            try:
                intention = "Continued conversation"
                new_session = await self.session_manager.rotate_session(
                    conversation_id=ctx.conversation.conversation_id,
                    current_session=ctx.session,
                    intention=intention,
                    completion_reason="compacted",
                    compaction_summary=compaction_result.summary,
                )
                ROTATION_TOTAL.labels(trigger="automatic").inc()
            except Exception:
                logger.warning(
                    "agent: session rotation after auto-compaction failed",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                    exc_info=True,
                )
                return

            # Pre-populate new session cache with compaction summary
            if compaction_result.summary:
                try:
                    await self.session_cache.refresh(new_session)
                    await self.session_cache.apply_compaction(
                        new_session,
                        summary=compaction_result.summary,
                        compaction_seq=0,
                    )
                except Exception:
                    logger.warning(
                        "agent: failed to pre-populate cache after auto-compaction",
                        extra={"extra_data": {"new_session_id": new_session.session_id}},
                        exc_info=True,
                    )

        # Notify clients via event bus
        summary_preview = (compaction_result.summary or "")[:500]
        await self.event_bus.publish(
            Event(
                type=EventType.SESSION_COMPACTED,
                data={
                    "conversation_id": ctx.conversation.conversation_id,
                    "session_id": new_session.session_id,
                    "previous_session_id": ctx.session.session_id,
                    "summary_preview": summary_preview,
                    "method": compaction_result.method,
                    "turns_compacted": compaction_result.turns_compacted,
                },
            )
        )

        logger.info(
            "agent: auto-compaction completed",
            extra={
                "extra_data": {
                    "conversation_id": ctx.conversation.conversation_id,
                    "old_session_id": ctx.session.session_id,
                    "new_session_id": new_session.session_id,
                    "method": compaction_result.method,
                    "turns_compacted": compaction_result.turns_compacted,
                }
            },
        )

    def _raise_if_cancelled(self, ctx: StepContext) -> None:
        """Abort the current step when external control requested interruption."""
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            raise StepInterrupted("Step interrupted by external control")

    async def _set_interactive_pause_state(
        self,
        ctx: StepContext,
        *,
        pause_type: str,
        pause_payload: dict[str, Any],
    ) -> None:
        """Persist pause metadata for task-backed interactive waits."""
        if ctx.task_id is None or ctx.step_run_id is None or ctx.workflow_state is None:
            return

        ctx.workflow_state.status = "paused"
        ctx.workflow_state.current_step_status = "paused"
        ctx.workflow_state.pending_pause_type = pause_type
        ctx.workflow_state.pending_pause_payload = pause_payload

        from cognis.store.queries import (
            update_step_run,
            update_task_status,
            update_task_workflow_state,
        )

        async with self.session_manager.session_factory() as db_session:
            await update_step_run(db_session, ctx.step_run_id, status="paused")
            await update_task_status(db_session, ctx.task_id, "paused")
            await update_task_workflow_state(
                db_session,
                ctx.task_id,
                ctx.workflow_state.model_dump(mode="json"),
            )
            await db_session.commit()

    async def _clear_interactive_pause_state(self, ctx: StepContext) -> None:
        """Clear pause metadata after a task-backed interactive wait resumes."""
        if ctx.task_id is None or ctx.step_run_id is None or ctx.workflow_state is None:
            return

        ctx.workflow_state.status = "running"
        ctx.workflow_state.current_step_status = "running"
        ctx.workflow_state.pending_pause_type = None
        ctx.workflow_state.pending_pause_payload = None

        from cognis.store.queries import (
            update_step_run,
            update_task_status,
            update_task_workflow_state,
        )

        async with self.session_manager.session_factory() as db_session:
            await update_step_run(db_session, ctx.step_run_id, status="running")
            await update_task_status(db_session, ctx.task_id, "running")
            await update_task_workflow_state(
                db_session,
                ctx.task_id,
                ctx.workflow_state.model_dump(mode="json"),
            )
            await db_session.commit()

    def _get_recovered_step_response(self, ctx: StepContext) -> str | None:
        """Return a persisted step-input response recovered after restart."""
        if ctx.workflow_state is None or ctx.workflow_state.pending_pause_type != "step_input":
            return None
        payload = ctx.workflow_state.pending_pause_payload or {}
        step_name = payload.get("step_name")
        if step_name is not None and step_name != ctx.step_definition.name:
            return None
        response = payload.get("response")
        if response is None:
            return None
        return str(response)

    def _build_step_prompt(self, ctx: StepContext) -> str:
        """Build the step objective prompt.

        For first-attempt workflow steps the ``StepContextAssembler``
        handles prior-step input injection, so this method only includes
        the task context, step objective, and any in-progress todos.

        For retries the regular ``ContextAssembler`` reads session history
        directly, so there is no step_inputs section here either.
        """
        parts: list[str] = []

        # Inject task context so the LLM knows what the workflow is about
        if ctx.task_title or ctx.task_description:
            parts.append("## Task\n\n")
            if ctx.task_title:
                parts.append(f"**{ctx.task_title}**\n\n")
            if ctx.task_description:
                parts.append(f"{ctx.task_description}\n\n")

        prompt_text = ctx.user_message or ctx.step_definition.prompt
        parts.append(f"## Step: {ctx.step_definition.name}\n\n{prompt_text}")

        if ctx.todos:
            parts.append("\n\n## Your step todos:\n")
            for todo in ctx.todos:
                status = todo.get("status", "pending")
                content = todo.get("content", "")
                parts.append(f"- [{status}] {content}")

        return "".join(parts)

    def _build_controller_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Build JSON schemas for controller-injected tools."""
        from cognis.tools.builtin.orchestration import orchestration_tools

        tools: list[dict[str, Any]] = []

        # step_complete — available for non-direct steps (sub-sessions, workflow steps)
        if not ctx.is_direct:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": STEP_COMPLETE,
                        "description": (
                            "Signal that this step is complete. You MUST call this "
                            "when the objective is satisfied. All todos must be "
                            "completed or cancelled before calling this."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "summary": {
                                    "type": "string",
                                    "description": "Brief summary of accomplishments",
                                },
                                "outputs": {
                                    "type": "object",
                                    "description": "Structured output data",
                                },
                                "claims": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Specific claims about what was done",
                                },
                            },
                            "required": ["summary"],
                        },
                    },
                }
            )

        # step_request_input — conditional
        if ctx.interaction_mode == "step_requests" and ctx.step_definition.allow_questions:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": STEP_REQUEST_INPUT,
                        "description": "Request input from the caller while staying in the same step.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "What you need to know",
                                },
                                "options": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional structured options",
                                },
                                "context": {
                                    "type": "string",
                                    "description": "Why you need this input",
                                },
                            },
                            "required": ["question"],
                        },
                    },
                }
            )

        # step_todo tools — always available
        tools.extend(
            [
                {
                    "type": "function",
                    "function": {
                        "name": STEP_TODO_WRITE,
                        "description": (
                            "Track progress within this step. Use status 'cancelled' "
                            "to mark todos that are no longer needed."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "todos": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "content": {"type": "string"},
                                            "status": {
                                                "type": "string",
                                                "enum": [
                                                    "pending",
                                                    "in_progress",
                                                    "done",
                                                    "cancelled",
                                                ],
                                            },
                                        },
                                    },
                                    "description": "Updated todo list",
                                },
                            },
                            "required": ["todos"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": STEP_TODO_LIST,
                        "description": "Read current step todos.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ]
        )

        # Orchestration tools — based on orchestration_mode
        for tool_def in orchestration_tools(ctx.orchestration_mode):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_def.name,
                        "description": tool_def.description,
                        "parameters": tool_def.parameters,
                    },
                }
            )

        return tools

    def _get_executor_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Get tool schemas from the executor's tool registry.

        Returns schemas in the OpenAI function calling format.
        """
        registry = self._get_tool_registry(ctx)
        if registry is None:
            return []

        from cognis.tools.builtin.orchestration import ORCHESTRATION_TOOL_NAMES

        schemas: list[dict[str, Any]] = []
        for tool_def in registry.list_tools():
            # Skip controller and orchestration tools (handled separately)
            if tool_def.name in CONTROLLER_TOOLS or tool_def.name in ORCHESTRATION_TOOL_NAMES:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_def.name,
                        "description": tool_def.description,
                        "parameters": tool_def.parameters,
                    },
                }
            )
        return schemas

    def _get_tool_registry(self, ctx: StepContext) -> Any:
        """Get the tool registry for the current step."""
        if ctx.tool_registry is not None:
            return ctx.tool_registry
        return getattr(self.providers, "_tool_registry", None)

    def _get_executor(self, ctx: StepContext) -> Any:
        """Get the executor connection for the current step."""
        if ctx.executor_connection is not None:
            return ctx.executor_connection
        return getattr(self.providers, "_executor_connection", None)
