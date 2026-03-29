"""Agent loop engine — the step runner.

Runs a single step as a full agentic loop: context assembly, LLM calls
with streaming, tool execution via router, and step finalization. This
is the heart of Cognis.

Used directly for main chat (Direct workflow) and by the WorkflowEngine
for multi-step background tasks.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Histogram

from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionEvent, SessionModel
from cognis.models.tool import ToolCall
from cognis.models.workflow import StepDefinition, StepOutput, WorkflowState
from cognis.runtime_context import scoped_runtime_context  # noqa: F401 — used in delegation
from cognis.tools.builtin.orchestration import (
    handle_orchestration_tool_call,
    is_orchestration_tool,
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

# Controller-injected tool names
STEP_COMPLETE = "step_complete"
STEP_REQUEST_INPUT = "step_request_input"
STEP_TODO_WRITE = "step_todo_write"
STEP_TODO_LIST = "step_todo_list"
CONTROLLER_TOOLS = {STEP_COMPLETE, STEP_REQUEST_INPUT, STEP_TODO_WRITE, STEP_TODO_LIST}

# Callback types
TokenCallback = Callable[[str], Coroutine[Any, Any, None]]
ToolCallCallback = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, None]]
ToolResultCallback = Callable[[str, str, str, bool, int | None], Coroutine[Any, Any, None]]

# Default limits
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_STEP_TIMEOUT_SECONDS = 600  # 10 minutes
_MAX_TOOL_DATA_BYTES = 10_240  # 10 KB truncation limit for tool args/results


def _truncate_tool_data(text: str) -> str:
    """Truncate tool data to a bounded size for WS events and Intaris recording."""
    if len(text) <= _MAX_TOOL_DATA_BYTES:
        return text
    return text[:_MAX_TOOL_DATA_BYTES] + f"\n... (truncated, {len(text)} bytes total)"


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
    ) -> list[PendingPause]:
        """List unresolved pauses, optionally filtered by task or session."""
        result: list[PendingPause] = []
        for pause in self._pending.values():
            if pause.resolved:
                continue
            if task_id is not None and pause.task_id != task_id:
                continue
            if session_id is not None and pause.session_id != session_id:
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
    disable_orchestration: bool = False  # True for child sessions (prevent recursive delegation)


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
        except Exception:
            logger.exception(
                "Agent loop step failed",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="error").inc()
            return None
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

    async def _run_child_session(
        self,
        *,
        child_session: SessionModel,
        conversation: ConversationModel,
        agent: AgentDefinition,
        task_description: str,
        mode: str,
        parent_intaris_session_id: str,
        tool_registry: Any,
        executor_connection: Any,
    ) -> None:
        """Run a delegated child session in the background.

        Executes a single agent-loop turn on the child session using the
        task description as the user message.  On completion (or failure)
        the result is recorded as an Intaris event in the **parent**
        session and a ``DELEGATION_COMPLETED`` / ``DELEGATION_FAILED``
        event is published so the frontend can update the delegation card.
        """
        conversation_id = conversation.conversation_id
        child_session_id = child_session.session_id

        # Resolve the correct agent if the child uses a different one (#3)
        resolved_agent = await self._resolve_child_agent(child_session.agent_id, agent)

        child_step = StepDefinition(name="delegation", type="run", prompt=task_description)
        child_ctx = StepContext(
            step_definition=child_step,
            session=child_session,
            conversation=conversation,
            agent=resolved_agent,
            is_direct=True,
            user_message=task_description,
            system_initiated=True,
            interaction_mode="explicit_gates",
            tool_registry=tool_registry,
            executor_connection=executor_connection,
            disable_orchestration=True,  # Prevent recursive delegation (#11)
        )

        # Set runtime context for JWT headers (#14)
        with scoped_runtime_context(
            user_email=child_session.user_email,
            agent_id=resolved_agent.agent_id,
        ):
            try:
                output = await self.run_step(child_ctx)
                result_summary = output.summary if output and output.summary else "Completed."

                # Update child session status — guarded (#7)
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

                # Record result in parent Intaris session — guarded (#7)
                # Use type="delegation" with status field (Intaris whitelist)
                try:
                    await self.providers.guardrails.record_events(
                        session_id=parent_intaris_session_id,
                        events=[
                            SessionEvent(
                                type="delegation",
                                data={
                                    "status": "completed",
                                    "child_session_id": child_session_id,
                                    "mode": mode,
                                    "result_summary": result_summary,
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

                # Always publish event bus event for frontend (#7)
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
                # Trigger a follow-up turn in the parent conversation
                await self.event_bus.publish(
                    Event(
                        type=EventType.FOLLOW_UP_TURN_REQUESTED,
                        data={
                            "conversation_id": conversation_id,
                            "status": "completed",
                        },
                    )
                )
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
                # Each operation guarded independently (#7)
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
                                    "mode": mode,
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

                # Always publish event bus event for frontend (#7)
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
                # Trigger a follow-up turn in the parent conversation
                await self.event_bus.publish(
                    Event(
                        type=EventType.FOLLOW_UP_TURN_REQUESTED,
                        data={
                            "conversation_id": conversation_id,
                            "status": "failed",
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
        step_output: StepOutput | None = None
        events_to_record: list[SessionEvent] = []
        messages: list[dict[str, Any]] = []

        # Build tool definitions for LLM (controller-injected tools)
        controller_tool_schemas = self._build_controller_tool_schemas(ctx)

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

        # Record user message event for direct workflow
        if ctx.is_direct and ctx.user_message and not ctx.system_initiated:
            events_to_record.append(
                SessionEvent(type="user_message", data={"content": ctx.user_message})
            )

        # Main agentic loop
        reprompted = False
        while True:
            self._raise_if_cancelled(ctx)

            # Stream LLM response
            accumulator = StreamAccumulator()
            async for chunk in self.providers.llm.stream_generate(
                messages,
                task_type="default",
                tools=controller_tool_schemas + self._get_executor_tool_schemas(ctx),
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

            # No tool calls — check if step is complete
            if not tool_calls:
                if ctx.is_direct:
                    # Direct workflow: step completes when LLM finishes
                    step_output = StepOutput(
                        summary=content[:500] if content else "",
                        outputs={},
                        claims=[],
                    )
                    break
                elif not reprompted:
                    # Re-prompt once for missing step_complete
                    STEP_REPROMPTS.inc()
                    reprompted = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You must call step_complete to finish this workflow step. "
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
                    step_output = StepOutput(
                        summary=tc.arguments.get("summary", ""),
                        outputs=tc.arguments.get("outputs", {}),
                        claims=tc.arguments.get("claims", []),
                    )
                    events_to_record.append(
                        SessionEvent(type="step_complete", data={"summary": step_output.summary})
                    )
                    result_content = json.dumps({"status": "completed"})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None)
                    break

                elif tc.name == STEP_TODO_WRITE:
                    ctx.todos = tc.arguments.get("todos", [])
                    result_content = json.dumps({"status": "updated", "count": len(ctx.todos)})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None)
                    continue

                elif tc.name == STEP_TODO_LIST:
                    result_content = json.dumps({"todos": ctx.todos})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None)
                    continue

                elif tc.name == STEP_REQUEST_INPUT:
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
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None)
                        continue

                    recovered_response = self._get_recovered_step_response(ctx)
                    if recovered_response is not None:
                        await self._clear_interactive_pause_state(ctx)
                        rec_content = json.dumps({"response": recovered_response})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": rec_content}
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, rec_content, False, None)
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
                            raise StepInterrupted("Step input request cancelled")
                        resp_content = json.dumps({"response": resolution.data.get("response", "")})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": resp_content}
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, resp_content, False, None)
                    except TimeoutError:
                        await self._clear_interactive_pause_state(ctx)
                        timeout_content = json.dumps({"error": "Input request timed out."})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": timeout_content}
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, timeout_content, True, None)
                    continue

                elif is_orchestration_tool(tc.name):
                    # Orchestration tool — intercept as controller directive
                    task_description = tc.arguments.get("task") or tc.arguments.get("reason", "")

                    # Prevent recursive delegation in child sessions (#11)
                    if ctx.disable_orchestration:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.call_id,
                                "content": json.dumps(
                                    {
                                        "status": "error",
                                        "message": "Delegation is not available in sub-sessions. "
                                        "Complete the task directly.",
                                    }
                                ),
                            }
                        )
                        continue

                    result, child_session = await handle_orchestration_tool_call(
                        tc,
                        session_manager=self.session_manager,
                        session=ctx.session,
                        agent=ctx.agent,
                    )

                    # Record delegation event AFTER child creation so we have
                    # child_session_id for consistent ID matching (#1, #8)
                    if child_session is not None:
                        parent_intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
                        # Add to batch (correct ordering with user_message)
                        events_to_record.append(
                            SessionEvent(
                                type="delegation",
                                data={
                                    "mode": tc.name,
                                    "call_id": tc.call_id,
                                    "task": task_description,
                                    "child_session_id": child_session.session_id,
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
                                    "mode": tc.name,
                                    "agent_id": child_session.agent_id,
                                    "task": task_description,
                                },
                            )
                        )

                        # Spawn background execution — retain task ref (#9)
                        _child_task = asyncio.create_task(
                            self._run_child_session(
                                child_session=child_session,
                                conversation=ctx.conversation,
                                agent=ctx.agent,
                                task_description=task_description,
                                mode=tc.name,
                                parent_intaris_session_id=parent_intaris_id,
                                tool_registry=ctx.tool_registry,
                                executor_connection=ctx.executor_connection,
                            )
                        )
                        _child_task.add_done_callback(
                            lambda t: t.exception() if not t.cancelled() else None
                        )
                        DELEGATIONS_TOTAL.labels(status="spawned").inc()
                        delegation_spawned = True
                    else:
                        # Child creation failed — record error event (#8)
                        events_to_record.append(
                            SessionEvent(
                                type="delegation",
                                data={
                                    "mode": tc.name,
                                    "call_id": tc.call_id,
                                    "task": task_description,
                                    "error": "Child session creation failed",
                                },
                            )
                        )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.call_id,
                            "content": result.output,
                        }
                    )
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

                    truncated_output = _truncate_tool_data(result.output)
                    events_to_record.append(
                        SessionEvent(
                            type="tool_result",
                            data={
                                "call_id": tc.call_id,
                                "name": tc.name,
                                "is_error": result.is_error,
                                "duration_ms": result.duration_ms,
                                "result": truncated_output,
                            },
                        )
                    )
                    if on_tool_result:
                        await on_tool_result(
                            tc.call_id,
                            tc.name,
                            truncated_output,
                            result.is_error,
                            result.duration_ms,
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

            # Check if step_complete was called in this batch
            if step_output is not None:
                break

            # Delegation spawned — end the parent turn after processing
            # the full tool batch.  The child runs in the background and
            # a follow-up turn will be triggered on completion.
            if delegation_spawned:
                step_output = StepOutput(
                    summary="Delegation spawned — working in background.",
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

        # Finalize step
        await self._finalize_step(ctx, events_to_record)

        if step_output:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="completed").inc()
        else:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="failed").inc()

        return step_output

    async def _finalize_step(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
    ) -> None:
        """Record events to Intaris, update cache, dispatch remember."""
        if not events:
            return

        idempotency_key = f"step_{ctx.session.session_id}_{uuid.uuid4().hex[:8]}"

        try:
            append_result = await self.providers.guardrails.record_events(
                session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                events=events,
                source="cognis",
                idempotency_key=idempotency_key,
            )
            # Update session cache with recorded events
            await self.session_cache.append_recorded_events(ctx.session, events, append_result)
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
        except Exception:
            logger.exception(
                "agent: failed to record events to Intaris",
                extra={
                    "extra_data": {"session_id": ctx.session.session_id, "event_count": len(events)}
                },
            )

        # Dispatch remember to retry queue
        assistant_content = " ".join(
            e.data.get("content", "")
            for e in events
            if e.type == "assistant_message" and e.data.get("content")
        )
        if assistant_content and ctx.session.mnemory_session_id:
            await self.remember_queue.enqueue(
                {
                    "session_id": ctx.session.mnemory_session_id,
                    "messages": [{"role": "assistant", "content": assistant_content[:5000]}],
                }
            )

        # Check compaction
        if ctx.session.intaris_session_id:
            entry = self.session_cache.get_entry(ctx.session.session_id)
            if entry and len(entry.events) > 50:
                try:
                    await self.compaction_strategy.compact(ctx.session)
                except Exception:
                    logger.warning(
                        "Compaction failed during step finalization",
                        extra={"extra_data": {"session_id": ctx.session.session_id}},
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
        the step objective and any in-progress todos.

        For retries the regular ``ContextAssembler`` reads session history
        directly, so there is no step_inputs section here either.
        """
        prompt_text = ctx.user_message or ctx.step_definition.prompt
        parts = [f"## Step: {ctx.step_definition.name}\n\n{prompt_text}"]

        if ctx.todos:
            parts.append("\n\n## Your step todos:\n")
            for todo in ctx.todos:
                status = todo.get("status", "pending")
                content = todo.get("content", "")
                parts.append(f"- [{status}] {content}")

        return "".join(parts)

    def _build_controller_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Build JSON schemas for controller-injected tools."""
        tools: list[dict[str, Any]] = []

        # step_complete — always available for run steps (except direct)
        if not ctx.is_direct:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": STEP_COMPLETE,
                        "description": (
                            "Signal that this workflow step is complete. You MUST call this "
                            "when the step objective is satisfied."
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
                        "description": "Track progress within this step. Survives compaction.",
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
                                                "enum": ["pending", "in_progress", "done"],
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

        return tools

    def _get_executor_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Get tool schemas from the executor's tool registry.

        Returns schemas in the OpenAI function calling format.
        """
        registry = self._get_tool_registry(ctx)
        if registry is None:
            return []

        schemas: list[dict[str, Any]] = []
        for tool_def in registry.list_tools():
            # Skip controller tools (handled separately)
            if tool_def.name in CONTROLLER_TOOLS:
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
