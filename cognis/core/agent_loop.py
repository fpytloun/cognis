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
import hashlib
import json
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter, Histogram
from pydantic import ValidationError

from cognis.core.attachment_utils import (
    merge_content_and_attachment_note,
    normalize_attachment_refs,
    strip_attachment_payload_bytes,
)
from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.decision import build_routing_reminder
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import FollowUpMetadata, FollowUpMode, FollowUpPolicy
from cognis.core.prompts import PromptContext
from cognis.core.pruning import prune_tool_outputs
from cognis.core.runtime import ExecutorEnvironmentSnapshot, ResolvedStepRuntime
from cognis.core.title_policy import sync_intaris_title
from cognis.core.tool_exposure import prepare_tool_exposure
from cognis.core.truncation import middle_truncate
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import AttachmentRef
from cognis.models.session import ConversationModel, SessionEvent, SessionModel
from cognis.models.tool import (
    Permission,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolSource,
    stable_tool_id,
    tool_display_name,
    tool_matches_identifier,
)
from cognis.models.workflow import StepDefinition, StepOutput, WorkflowState
from cognis.providers.retry import is_retryable_http_error
from cognis.runtime_context import scoped_runtime_context  # noqa: F401 — used in delegation
from cognis.store.queries import get_setting_value
from cognis.tools.builtin.orchestration import (
    OrchestrationMode,
    handle_delegate_tool_call,
    is_orchestration_tool,
    is_subsession_tool,
    is_task_tool,
    is_workflow_tool,
)
from cognis.tools.builtin.tool_search import SEARCH_TOOLS_TOOL, search_inventory

logger = get_logger(__name__)

_BOOTSTRAP_INTENTION_WAIT_MS = 1500
_INTARIS_RETRY_POLL_SECONDS = 5.0


def _user_message_for_recording(content: str, attachments: list[AttachmentRef]) -> str:
    if content.strip():
        return content
    if not attachments:
        return content
    return "User attached files."


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
REQUEST_CREDENTIAL = "request_credential"
REQUEST_AUTH_CHALLENGE = "request_auth_challenge"
LIST_CREDENTIALS = "list_credentials"
STEP_TODO_WRITE = "step_todo_write"
STEP_TODO_LIST = "step_todo_list"
CONTROLLER_TOOLS = {
    STEP_COMPLETE,
    STEP_REQUEST_INPUT,
    REQUEST_CREDENTIAL,
    REQUEST_AUTH_CHALLENGE,
    LIST_CREDENTIALS,
    STEP_TODO_WRITE,
    STEP_TODO_LIST,
    SEARCH_TOOLS_TOOL.name,
}

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


def _normalize_todo_status(status: Any) -> str:
    """Return the canonical status for persisted todo entries."""

    if status == "done":
        return "completed"
    return status if isinstance(status, str) else "pending"


def _normalize_todos(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize todo payloads so old persisted values still work."""

    normalized: list[dict[str, Any]] = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        item = dict(todo)
        item["status"] = _normalize_todo_status(item.get("status"))
        normalized.append(item)
    return normalized


def _truncate_tool_data(text: str) -> str:
    """Truncate tool data to a bounded size for WS events."""
    if len(text) <= _MAX_TOOL_DATA_BYTES:
        return text
    return text[:_MAX_TOOL_DATA_BYTES] + f"\n... (truncated, {len(text)} bytes total)"


def _step_complete_example_payload() -> dict[str, Any]:
    """Return a minimal valid ``step_complete`` payload example."""

    return {
        "summary": "Summarize what the step accomplished.",
        "outputs": {"key": "value"},
        "claims": ["State the verifiable deliverables from the written output."],
        "outcome": {
            "status": "success",
        },
    }


def _task_row_to_model(task_row: Any) -> Any:
    """Convert a task row into a ``TaskModel`` payload."""

    from cognis.models.task import TaskModel

    return TaskModel.model_validate(
        {
            "task_id": task_row.task_id,
            "title": getattr(task_row, "title", "Untitled task"),
            "description": getattr(task_row, "description", "") or "",
            "expected_output": getattr(task_row, "expected_output", None),
            "status": task_row.status,
            "priority": getattr(task_row, "priority", 0),
            "created_by": getattr(task_row, "created_by", "unknown@example.com"),
            "agent_id": task_row.agent_id,
            "source_type": getattr(task_row, "source_type", "agent"),
            "source_ref": getattr(task_row, "source_ref", None),
            "delivery": {
                "mode": getattr(task_row, "delivery_mode", "same_conversation"),
                "target": getattr(task_row, "delivery_target", None),
            },
            "workflow_id": getattr(task_row, "workflow_id", None),
            "workflow_state": getattr(task_row, "workflow_state", None),
            "queue_name": getattr(task_row, "queue_name", "default"),
            "scheduled_for": getattr(task_row, "scheduled_for", None),
            "created_at": getattr(task_row, "created_at", None),
            "started_at": getattr(task_row, "started_at", None),
            "completed_at": getattr(task_row, "completed_at", None),
            "result_summary": getattr(task_row, "result_summary", None),
            "result_data": getattr(task_row, "result_data", None),
        }
    )


def _workflow_registry_for_agent_loop(agent_loop: Any) -> Any:
    """Build a workflow registry from the session factory on demand."""

    from cognis.core.workflow_registry import WorkflowRegistry

    return WorkflowRegistry(agent_loop.session_manager.session_factory)


def _build_step_complete_validation_error(arguments: dict[str, Any], exc: ValidationError) -> str:
    """Build a structured validation error for malformed ``step_complete`` calls."""

    issues: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "payload"
        message = str(error.get("msg", "invalid value"))
        issues.append(f"{location}: {message}")

    return json.dumps(
        {
            "status": "rejected",
            "reason": "invalid_step_complete_arguments",
            "message": (
                "step_complete arguments did not match the required schema. "
                "Call step_complete again with corrected arguments."
            ),
            "issues": issues,
            "received": arguments,
            "example": _step_complete_example_payload(),
        }
    )


def _find_gate_revise_action(pause: PendingPause) -> str | None:
    """Extract the ``revise(step_name)`` action from a gate's options.

    Gate options are stored as ``[{"label": "Retry step", "action": "revise(research)"}]``
    in the PendingPause context. This returns the first ``revise(...)`` action
    found, which contains the correct original step name (not the synthetic
    gate name like ``research_exhausted``).
    """
    options = pause.options or []
    for opt in options:
        action = opt.get("action", "") if isinstance(opt, dict) else ""
        if isinstance(action, str) and action.startswith("revise("):
            return action
    return None


def _extract_operator_note(arguments: dict[str, Any]) -> str:
    """Extract an optional human operator note from task-pause tool arguments."""

    return str(arguments.get("note") or arguments.get("feedback") or "").strip()


def _append_tool_call_event(
    events: list[SessionEvent],
    tc: ToolCall,
    tool_id: str | None = None,
) -> None:
    """Record a tool_call event to the Intaris event batch."""
    events.append(
        SessionEvent(
            type="tool_call",
            data={
                "name": tc.name,
                "tool_id": tool_id,
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
    tool_id: str | None = None,
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
                "tool_id": tool_id,
                "is_error": is_error,
                "duration_ms": duration_ms,
                "result": truncated,
                "output_size": len(output),
                "has_full_output": False,
            },
        )
    )


def _controller_tool_definition(tool_name: str) -> ToolDefinition:
    return ToolDefinition(
        name=tool_name,
        description="Controller-managed tool",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(type="builtin"),
        category="system",
        read_only=True,
    )


def _tool_id_for_call(tool_name: str, registry: Any | None) -> str:
    if registry is not None:
        registered = registry.get(tool_name)
        if registered is not None:
            return stable_tool_id(registered.definition)
    return stable_tool_id(_controller_tool_definition(tool_name))


def _filter_model_inventory_tools(
    agent: AgentDefinition, tools: list[ToolDefinition], discovered_tool_ids: set[str] | None = None
) -> list[ToolDefinition]:
    filtered: list[ToolDefinition] = []
    permissions = agent.permissions
    visible_skill_tool_ids = _attached_skill_tool_ids(agent)
    if discovered_tool_ids:
        visible_skill_tool_ids.update(discovered_tool_ids)
    for tool in tools:
        if tool.name in CONTROLLER_TOOLS or is_orchestration_tool(tool.name):
            continue
        if tool.source.type == "skill" and stable_tool_id(tool) not in visible_skill_tool_ids:
            continue
        if (
            permissions is not None
            and permissions.resolve_permission(
                tool_display_name(tool), tool_id=stable_tool_id(tool)
            )
            is Permission.DENY
        ):
            continue
        filtered.append(tool)
    return filtered


def _attached_skill_tool_ids(agent: AgentDefinition) -> set[str]:
    if not isinstance(agent.skills, dict):
        return set()
    raw_ids = agent.skills.get("_attached_skill_tool_ids")
    if not isinstance(raw_ids, list):
        return set()
    return {str(tool_id) for tool_id in raw_ids if isinstance(tool_id, str) and tool_id.strip()}


def _controller_builtin_enabled(agent: AgentDefinition, tool: ToolDefinition) -> bool:
    if not isinstance(agent.tools, dict):
        return True
    disabled_categories = set(agent.tools.get("disabled_categories") or [])
    disabled_tools = set(agent.tools.get("disabled_tools") or [])
    if tool.category in disabled_categories or any(
        tool_matches_identifier(tool, identifier) for identifier in disabled_tools
    ):
        return False
    builtin_allow = agent.tools.get("builtin_tools")
    if not isinstance(builtin_allow, list):
        return True
    return "*" in builtin_allow or tool.name in builtin_allow


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
                # Attempt to recover concatenated JSON objects (e.g. from
                # bridge bugs that merge multiple tool calls into one index).
                split_args = _try_split_concatenated_json(tc["arguments"])
                if split_args is not None:
                    for i, parsed in enumerate(split_args):
                        result.append(
                            ToolCall(
                                call_id=tc["id"] or f"call_{uuid.uuid4().hex[:12]}",
                                name=tc["name"],
                                arguments=parsed,
                            )
                        )
                        if i == 0:
                            continue
                        # Generate unique call IDs for split-out tool calls
                        result[-1] = ToolCall(
                            call_id=f"call_{uuid.uuid4().hex[:12]}",
                            name=tc["name"],
                            arguments=parsed,
                        )
                    continue
                logger.warning(
                    "Malformed tool call arguments; passing as _raw",
                    extra={
                        "extra_data": {
                            "tool_name": tc["name"],
                            "args_length": len(tc["arguments"]),
                        }
                    },
                )
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


def _try_split_concatenated_json(raw: str) -> list[dict[str, Any]] | None:
    """Try to split a string containing multiple concatenated JSON objects.

    Returns a list of parsed dicts if successful, or ``None`` if the string
    does not look like concatenated JSON objects.
    """
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    results: list[dict[str, Any]] = []
    pos = 0
    while pos < len(raw):
        # Skip whitespace between objects
        while pos < len(raw) and raw[pos] in " \t\n\r":
            pos += 1
        if pos >= len(raw):
            break
        try:
            obj, end = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        results.append(obj)
        pos = end
    return results if len(results) > 1 else None


# ---------------------------------------------------------------------------
# Execution policy — replaces the is_direct boolean flag
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPolicy:
    """Controls behavioral differences between execution paths.

    Replaces the former ``is_direct`` boolean with explicit, named flags.
    Three presets cover the standard execution paths:

    - ``CHAT_POLICY``:  direct chat turns (WebSocket)
    - ``WORKFLOW_POLICY``:  background workflow steps
    - ``DELEGATION_POLICY``:  synchronous/async sub-sessions
    """

    require_step_complete: bool = False
    """Whether the LLM must call ``step_complete`` to signal completion."""

    step_complete_available: bool = False
    """Whether the ``step_complete`` tool is exposed to the LLM."""

    enable_auto_compaction: bool = False
    """Whether automatic context compaction runs after the turn."""

    event_flush_strategy: str = "batch"
    """``"batch"`` = single write at turn end; ``"incremental"`` = after each tool batch."""

    skip_memory: bool = False
    """Skip Mnemory recall/remember and memory instructions in context assembly."""

    skip_orchestration: bool = False
    """Skip orchestration tools (delegate, spawn_worker, tasks)."""


CHAT_POLICY = ExecutionPolicy(
    require_step_complete=False,
    step_complete_available=False,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
)

WORKFLOW_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=False,
    event_flush_strategy="incremental",
)

DELEGATION_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=False,
    event_flush_strategy="incremental",
)

SECONDARY_POLICY = ExecutionPolicy(
    require_step_complete=True,
    step_complete_available=True,
    enable_auto_compaction=True,
    event_flush_strategy="incremental",
    skip_memory=True,
    skip_orchestration=True,
)


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
    task_expected_output: str | None = None
    step_run_id: str | None = None
    policy: ExecutionPolicy = field(default_factory=lambda: CHAT_POLICY)
    is_retry: bool = False  # True for re-attempt within the same step
    user_message: str = ""
    user_attachments: list[AttachmentRef] = field(default_factory=list)
    attachment_notice: str | None = None
    prior_context: list[dict[str, Any]] | None = None  # Prior step output messages
    interaction_mode: str = "explicit_gates"
    tool_registry: Any = None  # ToolRegistry instance for this step
    executor_connection: Any = None  # ExecutorConnection for this step
    executor_environment: ExecutorEnvironmentSnapshot | None = None
    workflow_state: WorkflowState | None = None
    workflow_steps: list[StepDefinition] | None = None  # All steps for source resolution
    step_index: int = 0  # Index of current step in workflow
    cancel_event: asyncio.Event | None = None
    system_initiated: bool = False
    follow_up: FollowUpMetadata | None = None
    bootstrap_wait_for_intention: bool = False
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
        tool_output_store: Any = None,
        step_context_assembler: Any = None,  # DEPRECATED — kept for backward compat
        step_runtime_factory: Any = None,
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
        self.tool_output_store = tool_output_store
        self.notification_service: Any = None
        self._task_queue: Any = None
        self._step_runtime_factory = step_runtime_factory
        self._follow_up_policy = FollowUpPolicy(llm=getattr(providers, "llm", None))
        # Emergency event flush support — tracks pending events across
        # _execute_step and run_step for exception-path persistence.
        self._pending_events: list[SessionEvent] | None = None
        self._pending_events_ctx: StepContext | None = None
        # Track active child sessions per parent session for /stop cancellation
        self._active_children: dict[str, dict[str, asyncio.Task[Any]]] = {}
        self._children_lock = asyncio.Lock()

    def set_task_queue(self, task_queue: Any) -> None:
        """Wire the task queue after construction (breaks circular dependency).

        Must be called before the first agent turn so that controller tools
        ``create_task`` and ``cancel_task`` can submit/cancel via the queue.
        """
        self._task_queue = task_queue

    def set_step_runtime_factory(self, step_runtime_factory: Any) -> None:
        """Wire the step runtime factory after construction when needed."""

        self._step_runtime_factory = step_runtime_factory

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
                    "policy": ctx.policy.event_flush_strategy,
                }
            },
        )
        await self.session_lock.acquire(ctx.session.session_id)
        try:
            return await self._execute_step(
                ctx, on_token=on_token, on_tool_call=on_tool_call, on_tool_result=on_tool_result
            )
        except StepInterrupted:
            # Emergency flush: persist any accumulated events before
            # the cancellation propagates — events represent real work.
            await self._emergency_flush_events(ctx, self._pending_events)
            raise
        except Exception as exc:
            logger.exception(
                "Agent loop step failed",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            # Record a system_notice so the failure is visible in the
            # step session logs (UI chat timeline).
            error_msg = f"{type(exc).__name__}: {exc}"
            self._pending_events.append(
                SessionEvent(
                    type="lifecycle",
                    data={
                        "event": "system_notice",
                        "message": f"Step failed: {error_msg[:500]}",
                    },
                )
            )
            # Emergency flush: persist any accumulated events (including
            # the error notice) before reporting failure.
            await self._emergency_flush_events(ctx, self._pending_events)
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="error").inc()
            # Return a StepOutput with the error so it can be stored and
            # displayed in the UI instead of silently returning None.
            return StepOutput(
                summary=f"Step failed: {type(exc).__name__}",
                error=error_msg[:2000],
                session_id=ctx.session.session_id,
                intaris_session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                completed_at=datetime.now(UTC),
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

    async def _resolve_child_runtime(
        self,
        *,
        agent: AgentDefinition,
        user_email: str,
        fallback_tool_registry: Any,
        fallback_executor_connection: Any,
    ) -> ResolvedStepRuntime:
        """Resolve a fresh runtime for delegated child sessions when possible."""

        if callable(self._step_runtime_factory):
            return await self._step_runtime_factory(agent=agent, user_email=user_email)

        async def _noop_cleanup() -> None:
            return None

        return ResolvedStepRuntime(
            tool_registry=fallback_tool_registry,
            executor_connection=fallback_executor_connection,
            cleanup=_noop_cleanup,
            executor_environment=None,
        )

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
        child_runtime = await self._resolve_child_runtime(
            agent=resolved_agent,
            user_email=child_session.user_email,
            fallback_tool_registry=tool_registry,
            fallback_executor_connection=executor_connection,
        )

        child_step = StepDefinition(name="delegation", type="run", prompt=task_description)
        child_ctx = StepContext(
            step_definition=child_step,
            session=child_session,
            conversation=conversation,
            agent=resolved_agent,
            policy=DELEGATION_POLICY,
            user_message=task_description,
            system_initiated=True,
            interaction_mode="explicit_gates",
            tool_registry=child_runtime.tool_registry,
            executor_connection=child_runtime.executor_connection,
            executor_environment=child_runtime.executor_environment,
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
                    await self.session_manager.mark_completed(
                        child_session_id,
                        result_summary=result_summary,
                    )
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
                    await self.session_manager.mark_failed(
                        child_session_id,
                        result_summary="Delegation failed",
                    )
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
            finally:
                await child_runtime.cleanup()

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
            result_summary = output.summary if output else None
        except Exception:
            status = "failed"
            result_summary = None
        finally:
            await self._untrack_child(parent_session_id, child_session_id)

        delivery_id: str | None = None
        channel_deliverable = False
        delivery_fallback_text: str | None = None
        try:
            async with self.session_manager.session_factory() as db_session:
                from cognis.store.queries import (
                    create_channel_delivery_outbox,
                    get_conversation_channel_route,
                )

                route = await get_conversation_channel_route(db_session, conversation_id)
                if route is not None:
                    channel_type, account_id, chat_id, thread_id, user_email = route
                    delivery_id = f"cdel_{uuid.uuid4().hex[:12]}"
                    delivery_fallback_text = {
                        "completed": "Background work completed. I could not deliver the detailed reply, so please open the conversation for the full result.",
                        "failed": "Background work failed. I could not deliver the detailed reply, so please open the conversation for details.",
                    }[status]
                    await create_channel_delivery_outbox(
                        db_session,
                        delivery_id=delivery_id,
                        user_email=user_email,
                        conversation_id=conversation_id,
                        session_id=child_session_id,
                        source_type="delegation",
                        source_id=child_session_id,
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
                "delegation: failed to persist channel follow-up delivery intent",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "child_session_id": child_session_id,
                    }
                },
                exc_info=True,
            )

        follow_up = self._follow_up_policy.build_delegation_follow_up(
            conversation_id=conversation_id,
            child_session_id=child_session_id,
            status=status,
            result_summary=result_summary,
        )

        # Trigger a follow-up turn in the parent conversation
        await self.event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": conversation_id,
                    "follow_up": follow_up.model_dump(mode="json"),
                    "delivery_id": delivery_id,
                    "channel_deliverable": channel_deliverable,
                    "delivery_fallback_text": delivery_fallback_text,
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
        # Store on instance for emergency flush access from run_step
        self._pending_events = events_to_record
        self._pending_events_ctx = ctx
        messages: list[dict[str, Any]] = []
        assistant_content_parts: list[str] = []  # User-visible assistant output only
        assistant_memory_parts: list[str] = []  # Include attachment notes for memory/compaction
        last_assistant_content = ""

        # Build tool definitions for LLM (controller-injected tools)
        controller_tool_schemas = self._build_controller_tool_schemas(ctx)

        # ---------------------------------------------------------------
        # Step 1: Build effective_user_message BEFORE recording so that
        # Intaris always receives the full prompt the LLM will see (not
        # just the raw step prompt from ctx.user_message).
        # ---------------------------------------------------------------
        if ctx.policy.require_step_complete:
            if ctx.is_retry:
                # On retry with a reused Intaris session, the conversation
                # history already contains the original step prompt, all
                # the agent's prior work, and the evaluation feedback event.
                # Sending the full step prompt again would cause the agent
                # to see it multiple times and restart from scratch.
                #
                # Instead, send only a revision directive.  If the Intaris
                # recording of evaluation feedback failed, deliver it inline.
                if ctx.workflow_state and ctx.workflow_state.last_evaluation_feedback:
                    effective_user_message = (
                        "The evaluator has reviewed your previous attempt and "
                        "requested revisions:\n\n"
                        f"{ctx.workflow_state.last_evaluation_feedback}\n\n"
                        "Please revise your work based on this feedback. "
                        "When done, write out your updated findings and call "
                        "step_complete."
                    )
                    ctx.workflow_state.last_evaluation_feedback = None
                else:
                    # Feedback was recorded to Intaris — it's already in the
                    # session history.  Send a minimal revision directive.
                    effective_user_message = (
                        "The evaluator has reviewed your previous attempt and "
                        "requested revisions. Review the evaluation feedback "
                        "above and revise your work accordingly. When done, "
                        "write out your updated findings and call step_complete."
                    )
            else:
                # First attempt — build the full rich prompt with task context
                # and prior step outputs.
                effective_user_message = self._build_step_prompt(ctx)
        else:
            effective_user_message = ctx.user_message or ctx.step_definition.prompt

        # ---------------------------------------------------------------
        # Step 2: Record effective_user_message to Intaris BEFORE context
        # assembly so the IntentionBarrier can start updating the session
        # intention in parallel.  This records the FULL prompt (including
        # task context and prior step outputs for workflow steps), not
        # just the raw step prompt.
        #
        # Skipped for:
        # - system_initiated turns (lifecycle event provides the trail)
        # - retry turns (session already has the original prompt)
        # ---------------------------------------------------------------
        recorded_user_message = _user_message_for_recording(
            effective_user_message,
            ctx.user_attachments,
        )
        if recorded_user_message and not ctx.system_initiated and not ctx.is_retry:
            user_msg_event = SessionEvent(
                type="user_message",
                data={
                    "content": recorded_user_message,
                    "attachments": [
                        item.model_dump(mode="json", exclude={"url"})
                        for item in ctx.user_attachments
                    ],
                },
            )
            try:
                await self._record_events_strict(
                    ctx,
                    [user_msg_event],
                    reason="user_message",
                    on_token=on_token,
                )
            except Exception:
                logger.exception(
                    "agent: failed to record early user_message event",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
                raise

            try:
                reasoning_result = await self._report_reasoning_strict(ctx, on_token=on_token)
                if reasoning_result and reasoning_result.updated_at:
                    updated = await self.session_cache.update_intention(
                        ctx.session.session_id,
                        reasoning_result.intention,
                        updated_at=reasoning_result.updated_at,
                    )
                    if updated and reasoning_result.title:
                        try:
                            async with self.session_manager.session_factory() as db_session:
                                ok = await sync_intaris_title(
                                    db_session,
                                    ctx.conversation,
                                    reasoning_result.title,
                                )
                                if ok:
                                    await db_session.commit()
                        except Exception:
                            logger.debug(
                                "agent: failed to sync bootstrap title from Intaris",
                                extra={
                                    "extra_data": {
                                        "conversation_id": ctx.conversation.conversation_id,
                                    }
                                },
                                exc_info=True,
                            )
            except Exception:
                logger.exception(
                    "agent: failed to trigger intention update",
                    extra={"extra_data": {"session_id": ctx.session.session_id}},
                )
                raise

        # ---------------------------------------------------------------
        # Step 3: Assemble context (reads Intaris history + memory)
        # ---------------------------------------------------------------
        # Derive prompt context from execution policy
        if ctx.policy is WORKFLOW_POLICY:
            _prompt_ctx = PromptContext.TASK_STEP
        elif ctx.policy is DELEGATION_POLICY:
            _prompt_ctx = PromptContext.DELEGATION
        elif ctx.follow_up is not None and ctx.follow_up.mode is FollowUpMode.INTEGRATE:
            _prompt_ctx = PromptContext.FOLLOW_UP_INTEGRATE
        elif ctx.follow_up is not None:
            _prompt_ctx = PromptContext.FOLLOW_UP_NOTIFY
        else:
            _prompt_ctx = PromptContext.CHAT

        routing_reminder = None
        if (
            ctx.policy is CHAT_POLICY
            and _prompt_ctx is PromptContext.CHAT
            and not ctx.system_initiated
        ):
            advice = build_routing_reminder(effective_user_message)
            routing_reminder = advice.reminder if advice is not None else None

        context_result = await self.context_assembler.assemble(
            session=ctx.session,
            conversation=ctx.conversation,
            agent=ctx.agent,
            user_message=effective_user_message,
            user_attachments=[item.model_dump(mode="json") for item in ctx.user_attachments],
            attachment_notice=ctx.attachment_notice,
            user_message_role="system" if ctx.system_initiated else "user",
            prior_context=ctx.prior_context,
            follow_up=ctx.follow_up,
            routing_reminder=routing_reminder,
            skip_memory=ctx.policy.skip_memory,
            prompt_context=_prompt_ctx,
            executor_environment=ctx.executor_environment,
        )
        messages = context_result.messages

        # Record user message event (unless already recorded early for
        # intention tracking above).  System-initiated follow-up turns
        # (task completion, delegation results) are NOT recorded — the
        # lifecycle event already provides the audit trail and the prompt
        # is an internal instruction, not user-visible content.
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
        mid_stream_retries = 0
        _MAX_MID_STREAM_RETRIES = 2
        discovered_tool_ids = self._get_initial_discovered_tool_ids(ctx)
        collected_attachments: list[dict[str, Any]] = []
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
            # Chain: session override → workflow step default → agent config → provider default.
            model_for_llm = self.session_cache.get_model_override(ctx.session.session_id) or (
                ctx.agent.llm_config.model if ctx.agent.llm_config else None
            )

            reasoning_effort = (
                self.session_cache.get_reasoning_effort_override(ctx.session.session_id)
                or getattr(ctx.step_definition, "reasoning_effort", None)
                or (ctx.agent.llm_config.reasoning_effort if ctx.agent.llm_config else None)
            )

            llm_kwargs: dict[str, Any] = {}
            if reasoning_effort:
                llm_kwargs["reasoning_effort"] = reasoning_effort

            model_info = await self.providers.llm.get_model_info(model_for_llm or resolved_model)
            registry = self._get_tool_registry(ctx)
            inventory_tools = (
                _filter_model_inventory_tools(ctx.agent, registry.list_tools(), discovered_tool_ids)
                if registry is not None
                else []
            )
            exposure = prepare_tool_exposure(
                inventory_tools=inventory_tools,
                controller_tool_schemas=controller_tool_schemas,
                model=model_for_llm or resolved_model,
                model_info=model_info,
                discovered_tool_ids=discovered_tool_ids,
            )
            logger.debug(
                "Prepared tool exposure",
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        **exposure.debug_metadata,
                        "extra_header_keys": sorted(
                            (exposure.request_kwargs.get("extra_headers") or {}).keys()
                        ),
                    }
                },
            )
            llm_kwargs.update(exposure.request_kwargs)

            # Stream LLM response
            accumulator = StreamAccumulator()
            mid_stream_error: str | None = None
            async for chunk in self.providers.llm.stream_generate(
                messages,
                model=model_for_llm,
                task_type="default",
                tools=exposure.tools,
                cache_breakpoint_index=cache_breakpoint,
                **llm_kwargs,
            ):
                if chunk.get("mid_stream_failure"):
                    mid_stream_error = chunk.get("error", "LLM stream failed mid-generation")
                    break
                text_delta = accumulator.feed(chunk)
                if text_delta and on_token:
                    await on_token(text_delta)

            if mid_stream_error:
                if mid_stream_retries < _MAX_MID_STREAM_RETRIES:
                    mid_stream_retries += 1
                    logger.warning(
                        "agent: mid-stream failure, retrying LLM call (%d/%d)",
                        mid_stream_retries,
                        _MAX_MID_STREAM_RETRIES,
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "error": mid_stream_error[:200],
                            }
                        },
                    )
                    # Retry is transparent to the user — no visible message.
                    await asyncio.sleep(1.0 * mid_stream_retries)
                    continue  # retry — messages is clean, accumulator is fresh next iteration

                # Retries exhausted — capture partial content (if any) and
                # record a lifecycle event so the failure appears as a system
                # message in the UI, not as assistant text.
                partial_content = accumulator.get_content()
                if partial_content or collected_attachments:
                    events_to_record.append(
                        SessionEvent(
                            type="assistant_message",
                            data={
                                "content": partial_content,
                                "attachments": strip_attachment_payload_bytes(
                                    collected_attachments
                                ),
                            },
                        )
                    )
                    if partial_content:
                        assistant_content_parts.append(partial_content)
                    memory_text = merge_content_and_attachment_note(
                        partial_content,
                        strip_attachment_payload_bytes(collected_attachments),
                    )
                    if memory_text.strip():
                        assistant_memory_parts.append(memory_text)

                logger.warning(
                    "agent: mid-stream failure after retries exhausted",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "error": mid_stream_error[:200],
                        }
                    },
                )
                error_notice = (
                    "A model error occurred while generating the response. "
                    "Your tool results have been saved. Please try sending your message again."
                )
                events_to_record.append(
                    SessionEvent(
                        type="lifecycle",
                        data={"event": "system_notice", "message": error_notice},
                    )
                )
                if on_token:
                    await on_token(f"\n\n{error_notice}")
                if events_to_record:
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="mid_stream_failure",
                        on_token=on_token,
                    )
                break  # Exit while loop → _finalize_step runs normally

            content = accumulator.get_content()
            tool_calls = accumulator.get_tool_calls()
            for tc in tool_calls:
                mapped_name = exposure.alias_map.get(tc.name, tc.name)
                if mapped_name != tc.name:
                    logger.debug(
                        "Resolved tool alias",
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "visible_name": tc.name,
                                "internal_name": mapped_name,
                            }
                        },
                    )
                    tc.name = mapped_name

            # Record assistant message
            if content or collected_attachments:
                events_to_record.append(
                    SessionEvent(
                        type="assistant_message",
                        data={
                            "content": content,
                            "attachments": strip_attachment_payload_bytes(collected_attachments),
                        },
                    )
                )
                if content:
                    messages.append({"role": "assistant", "content": content})
                    assistant_content_parts.append(content)
                    last_assistant_content = content
                memory_text = merge_content_and_attachment_note(
                    content,
                    strip_attachment_payload_bytes(collected_attachments),
                )
                if memory_text.strip():
                    assistant_memory_parts.append(memory_text)
                await self._flush_events_incremental(
                    ctx,
                    events_to_record,
                    reason="assistant_message",
                    on_token=on_token,
                )

            # No tool calls — check if step is complete
            if not tool_calls:
                if not ctx.policy.require_step_complete:
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
                                "role": "system",
                                "content": (
                                    "Internal controller reminder — this is not a new user message. "
                                    "Do not write a filler acknowledgment just for this reminder.\n\n"
                                    f"You have {len(incomplete_todos)} incomplete todos:\n"
                                    f"{todo_list}\n\n"
                                    "These todos should represent only current-turn "
                                    "execution work that you still own. First decide "
                                    "whether work actually remains, or whether only your "
                                    "todo state is stale. If work remains, continue it, "
                                    "ask for input if needed, and only produce assistant "
                                    "text if you have new user-visible information, a "
                                    "required question, or a correction. If only todo "
                                    "cleanup remains, update or cancel the todos via "
                                    "step_todo_write and produce no assistant text. Do "
                                    "not repeat, restate, or paraphrase content that has "
                                    "already been sent to the user."
                                ),
                            }
                        )
                        continue
                    # Todos done (or max re-prompts reached) — complete
                    step_output = StepOutput(
                        summary=content[:500] if content else "",
                        content=content,
                        outputs={},
                        claims=[],
                        attachments=list(collected_attachments),
                        session_id=ctx.session.session_id,
                        intaris_session_id=ctx.session.intaris_session_id or ctx.session.session_id,
                        completed_at=datetime.now(UTC),
                    )
                    break
                elif not reprompted:
                    # Non-direct (sub-session / workflow step): require step_complete
                    STEP_REPROMPTS.inc()
                    reprompted = True
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Internal controller reminder — this is not a new user message. "
                                "Do not write a filler acknowledgment just for this reminder. "
                                "If the step is finished, call step_complete now with your summary. "
                                "Otherwise continue the work until it is actually complete."
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
                tool_id = _tool_id_for_call(tc.name, registry)
                STEP_TOOL_CALLS.labels(tool_name=tool_id).inc()

                if on_tool_call:
                    await on_tool_call(tc.name, tc.call_id, tc.arguments)

                # Controller tool interception
                if tc.name == STEP_COMPLETE:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:step_complete",
                        on_token=on_token,
                    )
                    # Reject step_complete when it's not available (e.g. direct chat)
                    if not ctx.policy.step_complete_available:
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
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    # Enforce todo completion for workflow steps
                    incomplete_todos = self._get_incomplete_todos(ctx)
                    if incomplete_todos and ctx.policy.require_step_complete:
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
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    try:
                        step_output = StepOutput(
                            summary=tc.arguments.get("summary", ""),
                            content=last_assistant_content,
                            outputs=tc.arguments.get("outputs", {}),
                            claims=tc.arguments.get("claims", []),
                            outcome=tc.arguments.get("outcome"),
                            attachments=list(collected_attachments),
                            session_id=ctx.session.session_id,
                            intaris_session_id=ctx.session.intaris_session_id
                            or ctx.session.session_id,
                            completed_at=datetime.now(UTC),
                        )
                    except ValidationError as exc:
                        err_content = _build_step_complete_validation_error(tc.arguments, exc)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_complete",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(tc.call_id, tc.name, err_content, True, None, None)
                        continue

                    events_to_record.append(
                        SessionEvent(
                            type="lifecycle",
                            data={
                                "event": "step_complete",
                                "status": "completed",
                                "summary": step_output.summary,
                                "outcome_status": (
                                    step_output.outcome.status if step_output.outcome else "success"
                                ),
                            },
                        )
                    )
                    result_content = json.dumps({"status": "completed"})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:step_complete",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    break

                elif tc.name == STEP_TODO_WRITE:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    ctx.todos = tc.arguments.get("todos", [])
                    result_content = json.dumps({"status": "updated", "count": len(ctx.todos)})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:step_todo_write",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == STEP_TODO_LIST:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    result_content = json.dumps({"todos": ctx.todos})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:step_todo_list",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == STEP_REQUEST_INPUT:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:step_request_input",
                        on_token=on_token,
                    )
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
                        _append_tool_result_event(
                            events_to_record, tc, err_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_input",
                            on_token=on_token,
                        )
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
                        _append_tool_result_event(
                            events_to_record, tc, rec_content, False, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_input",
                            on_token=on_token,
                        )
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
                    formatted_options = (
                        [{"label": option, "action": option} for option in pause_options]
                        if pause_options is not None
                        else None
                    )

                    # Create the step question via the notification service
                    # so it is persisted, resolved to the source conversation,
                    # and survives restarts.
                    await self.notification_service.create(
                        notification_type="step_question",
                        user_email=ctx.session.user_email,
                        conversation_id=ctx.conversation.conversation_id,
                        task_id=ctx.task_id,
                        step_name=ctx.step_definition.name,
                        step_run_id=ctx.step_run_id,
                        session_id=ctx.session.session_id,
                        notification_id=pause_id,
                        payload={
                            "question": question,
                            "options": formatted_options,
                            "context": pause_context,
                        },
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
                        _append_tool_result_event(
                            events_to_record, tc, resp_content, False, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_input",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, resp_content, False, None, None
                            )
                    except TimeoutError:
                        await self._clear_interactive_pause_state(ctx)
                        if self.notification_service is not None:
                            await self.notification_service.mark_orphaned(
                                pause_id,
                                reason="timeout",
                            )
                        timeout_content = json.dumps({"error": "Input request timed out."})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": timeout_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, timeout_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason="tool_result:step_request_input",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, timeout_content, True, None, None
                            )
                    continue

                elif tc.name in {REQUEST_CREDENTIAL, REQUEST_AUTH_CHALLENGE}:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_call:{tc.name}",
                        on_token=on_token,
                    )
                    pause_id = f"auth_{uuid.uuid4().hex[:12]}"
                    timeout_seconds = int(tc.arguments.get("timeout_seconds", 600) or 600)
                    payload = {
                        "credential_id": tc.arguments.get("credential_id"),
                        "kind": tc.arguments.get("kind"),
                        "scope": tc.arguments.get("scope", "user"),
                        "agent_id": tc.arguments.get("agent_id"),
                        "label": tc.arguments.get("label")
                        or tc.arguments.get("credential_id")
                        or "Authentication required",
                        "message": tc.arguments.get("message")
                        or tc.arguments.get("description")
                        or "Authentication is required to continue.",
                        "description": tc.arguments.get("description"),
                        "metadata": (
                            tc.arguments.get("metadata")
                            if isinstance(tc.arguments.get("metadata"), dict)
                            else {}
                        ),
                        "required_fields": (
                            tc.arguments.get("required_fields")
                            if isinstance(tc.arguments.get("required_fields"), list)
                            else []
                        ),
                        "expires_at": (
                            datetime.now(UTC) + timedelta(seconds=timeout_seconds)
                        ).isoformat(),
                    }
                    notification_type = (
                        "credential_request" if tc.name == REQUEST_CREDENTIAL else "auth_challenge"
                    )
                    await self.notification_service.create(
                        notification_type=notification_type,
                        user_email=ctx.session.user_email,
                        conversation_id=ctx.conversation.conversation_id,
                        task_id=ctx.task_id,
                        step_name=ctx.step_definition.name,
                        step_run_id=ctx.step_run_id,
                        session_id=ctx.session.session_id,
                        notification_id=pause_id,
                        payload=payload,
                    )
                    await self._set_interactive_pause_state(
                        ctx,
                        pause_type=notification_type,
                        pause_payload={
                            "pause_id": pause_id,
                            "step_name": ctx.step_definition.name,
                            "step_run_id": ctx.step_run_id,
                            "session_id": ctx.session.session_id,
                            **payload,
                        },
                    )
                    try:
                        resolution = await self.pause_waiter.wait(
                            pause_id, timeout=float(timeout_seconds)
                        )
                        await self._clear_interactive_pause_state(ctx)
                        if resolution.decision in {"cancel", "deny"}:
                            raise StepInterrupted("Authentication request cancelled")
                        if tc.name == REQUEST_CREDENTIAL and not resolution.data.get(
                            "credential_id"
                        ):
                            err_content = json.dumps(
                                {"error": "Credential request was not fulfilled."}
                            )
                            messages.append(
                                {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                            )
                            _append_tool_result_event(
                                events_to_record, tc, err_content, True, tool_id=tool_id
                            )
                            await self._flush_events_incremental(
                                ctx,
                                events_to_record,
                                reason=f"tool_result:{tc.name}",
                                on_token=on_token,
                            )
                            if on_tool_result:
                                await on_tool_result(
                                    tc.call_id, tc.name, err_content, True, None, None
                                )
                            continue
                        if tc.name == REQUEST_AUTH_CHALLENGE and not (
                            resolution.data.get("response_ref")
                            or resolution.data.get("challenge_completed")
                        ):
                            err_content = json.dumps({"error": "Auth challenge was not fulfilled."})
                            messages.append(
                                {"role": "tool", "tool_call_id": tc.call_id, "content": err_content}
                            )
                            _append_tool_result_event(
                                events_to_record, tc, err_content, True, tool_id=tool_id
                            )
                            await self._flush_events_incremental(
                                ctx,
                                events_to_record,
                                reason=f"tool_result:{tc.name}",
                                on_token=on_token,
                            )
                            if on_tool_result:
                                await on_tool_result(
                                    tc.call_id, tc.name, err_content, True, None, None
                                )
                            continue
                        resp_content = json.dumps(
                            {
                                "credential_id": resolution.data.get("credential_id"),
                                "credential_label": resolution.data.get("credential_label"),
                                "credential_kind": resolution.data.get("credential_kind"),
                                "response_ref": resolution.data.get("response_ref"),
                                "challenge_completed": resolution.data.get(
                                    "challenge_completed", False
                                ),
                            }
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.call_id, "content": resp_content}
                        )
                        _append_tool_result_event(
                            events_to_record, tc, resp_content, False, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_result:{tc.name}",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, resp_content, False, None, None
                            )
                    except TimeoutError:
                        await self._clear_interactive_pause_state(ctx)
                        if self.notification_service is not None:
                            await self.notification_service.mark_orphaned(
                                pause_id,
                                reason="timeout",
                            )
                        timeout_content = json.dumps({"error": "Authentication request timed out."})
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.call_id,
                                "content": timeout_content,
                            }
                        )
                        _append_tool_result_event(
                            events_to_record, tc, timeout_content, True, tool_id=tool_id
                        )
                        await self._flush_events_incremental(
                            ctx,
                            events_to_record,
                            reason=f"tool_result:{tc.name}",
                            on_token=on_token,
                        )
                        if on_tool_result:
                            await on_tool_result(
                                tc.call_id, tc.name, timeout_content, True, None, None
                            )
                    continue

                elif tc.name == LIST_CREDENTIALS:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:list_credentials",
                        on_token=on_token,
                    )
                    rows = await self.providers.credentials.list_credentials(ctx.session.user_email)
                    allowed = set(
                        ctx.agent.permissions.allowed_credentials if ctx.agent.permissions else []
                    )
                    kind_filter = str(tc.arguments.get("kind", "")).strip().lower()
                    domain_filter = str(tc.arguments.get("domain", "")).strip().lower()
                    origin_filter = str(tc.arguments.get("origin", "")).strip().lower()
                    label_filter = str(tc.arguments.get("label_contains", "")).strip().lower()
                    matches: list[dict[str, Any]] = []
                    for row in rows:
                        if row.credential_id not in allowed:
                            continue
                        metadata = row.metadata or {}
                        if kind_filter and str(row.kind).lower() != kind_filter:
                            continue
                        if (
                            domain_filter
                            and str(metadata.get("domain", "")).lower() != domain_filter
                        ):
                            continue
                        if (
                            origin_filter
                            and str(metadata.get("origin", "")).lower() != origin_filter
                        ):
                            continue
                        if label_filter and label_filter not in str(row.label).lower():
                            continue
                        matches.append(
                            {
                                "credential_id": row.credential_id,
                                "kind": row.kind,
                                "label": row.label,
                                "description": row.description,
                                "metadata": metadata,
                                "field_names": list(row.field_names or []),
                                "status": row.status,
                                "expires_at": row.expires_at.isoformat()
                                if row.expires_at
                                else None,
                            }
                        )
                    result_content = json.dumps({"credentials": matches})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:list_credentials",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif tc.name == SEARCH_TOOLS_TOOL.name:
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_call:search_tools",
                        on_token=on_token,
                    )
                    matches = search_inventory(
                        inventory_tools,
                        str(tc.arguments.get("query", "")),
                        category=(
                            str(tc.arguments.get("category"))
                            if tc.arguments.get("category") is not None
                            else None
                        ),
                        limit=int(tc.arguments.get("limit", 10) or 10),
                    )
                    discovered_tool_ids.update(
                        {
                            str(match["tool_id"])
                            for match in matches
                            if isinstance(match.get("tool_id"), str)
                        }
                    )
                    logger.debug(
                        "Tool discovery updated",
                        extra={
                            "extra_data": {
                                "session_id": ctx.session.session_id,
                                "query_length": len(str(tc.arguments.get("query", ""))),
                                "match_count": len(matches),
                                "discovered_tool_count": len(discovered_tool_ids),
                            }
                        },
                    )
                    result_content = json.dumps({"matches": matches})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "content": result_content}
                    )
                    _append_tool_result_event(
                        events_to_record, tc, result_content, False, tool_id=tool_id
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason="tool_result:search_tools",
                        on_token=on_token,
                    )
                    if on_tool_result:
                        await on_tool_result(tc.call_id, tc.name, result_content, False, None, None)
                    continue

                elif is_orchestration_tool(tc.name):
                    # Orchestration tool — intercept as controller directive
                    _append_tool_call_event(events_to_record, tc, tool_id)
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_call:{tc.name}",
                        on_token=on_token,
                    )
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
                        events_to_record,
                        tc,
                        orch_result.output,
                        orch_result.is_error,
                        tool_id=tool_id,
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_result:{tc.name}",
                        on_token=on_token,
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
                                "tool_id": tool_id,
                            },
                        )
                    )
                    events_to_record.append(
                        SessionEvent(
                            type="tool_call",
                            data={
                                "name": tc.name,
                                "tool_id": tool_id,
                                "call_id": tc.call_id,
                                "arguments": _truncate_tool_data(
                                    json.dumps(tc.arguments, default=str)
                                ),
                            },
                        )
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_call:{tool_id}",
                        on_token=on_token,
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
                    eval_meta = result.metadata.get("evaluation") if result.metadata else None
                    events_to_record.append(
                        SessionEvent(
                            type="tool_result",
                            data={
                                "call_id": tc.call_id,
                                "name": tc.name,
                                "tool_id": tool_id,
                                "is_error": result.is_error,
                                "duration_ms": result.duration_ms,
                                "result": intaris_preview,
                                "output_size": original_size or len(result.output),
                                "has_full_output": raw_output is not None,
                                "evaluation": eval_meta,
                            },
                        )
                    )
                    await self._flush_events_incremental(
                        ctx,
                        events_to_record,
                        reason=f"tool_result:{tool_id}",
                        on_token=on_token,
                    )
                    ws_preview = _truncate_tool_data(result.output)
                    if on_tool_result:
                        await on_tool_result(
                            tc.call_id,
                            tc.name,
                            ws_preview,
                            result.is_error,
                            result.duration_ms,
                            eval_meta,
                        )
                    if result.attachments:
                        collected_attachments.extend(normalize_attachment_refs(result.attachments))
                    if result.metadata:
                        self._merge_discovered_tool_ids(discovered_tool_ids, result.metadata)
                        self._apply_skill_attachment_metadata(ctx, result.metadata)
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
                                "tool_id": tool_id,
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
                    content="\n\n".join(assistant_content_parts),
                    attachments=list(collected_attachments),
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
                        "role": "system",
                        "content": (
                            "Internal controller reminder — this is not a new user message. "
                            "Do not write a filler acknowledgment just for this reminder. "
                            f"Tool call limit ({max_tool_calls}) reached. "
                            "If the step is finished, call step_complete now. Otherwise continue "
                            "only if you can finish without more tool calls."
                        ),
                    }
                )

        # Finalize step — pass assistant_content_parts so Mnemory remember
        # works even when events were already flushed incrementally.
        events_recorded = await self._finalize_step(
            ctx,
            events_to_record,
            assistant_content_parts=assistant_content_parts,
            assistant_memory_parts=assistant_memory_parts,
        )

        # Automatic compaction: if context assembly recommended compaction
        # and events were successfully recorded, compact + rotate session
        # so the next turn starts with a clean context window.  Only for
        # direct chat — workflow steps have their own lifecycle management.
        if (
            events_recorded
            and ctx.policy.enable_auto_compaction
            and getattr(context_result, "recommend_compaction", False)
        ):
            await self._auto_compact(ctx)

        if step_output:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="completed").inc()
        else:
            STEPS_TOTAL.labels(step_type=ctx.step_definition.type, status="failed").inc()

        self._pending_events = None
        self._pending_events_ctx = None
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

        conversation_id = ctx.conversation.conversation_id

        # Read escalation timeout from settings
        async with self.session_manager.session_factory() as db:
            timeout_raw: int = await get_setting_value(  # type: ignore[assignment]
                db, "session.escalation_timeout_seconds", 300
            )
        timeout_f = float(timeout_raw)

        # Create the escalation via the notification service.  For
        # task-originated escalations, the service resolves the target
        # to the task's source conversation so the user sees the
        # escalation in their chat, not in the invisible task step.
        # The intaris_call_id is used as notification_id so the existing
        # /escalations/{call_id}/resolve endpoint can find it.
        await self.notification_service.create(
            notification_type="escalation",
            user_email=ctx.session.user_email,
            conversation_id=conversation_id,
            task_id=ctx.task_id,
            session_id=ctx.session.session_id,
            notification_id=intaris_call_id,
            payload={
                "call_id": intaris_call_id,
                "tool_name": tc.name,
                "risk": eval_meta.get("risk"),
                "reasoning": eval_meta.get("reasoning"),
                "timeout_seconds": timeout_raw,
                "context": {
                    "call_id": intaris_call_id,
                    "tool_name": tc.name,
                },
            },
        )
        pause_id = intaris_call_id

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
            return await self._handle_task_tool(tc, ctx=ctx, events_to_record=events_to_record)
        elif is_workflow_tool(tc.name):
            return await self._handle_workflow_tool(tc, ctx=ctx)
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

        # Resolve agent registry for binding validation
        _agent_registry = None
        if hasattr(self.session_manager, "_session_factory"):
            from cognis.core.agent_registry import AgentRegistry

            _agent_registry = AgentRegistry(self.session_manager._session_factory)

        result, child_session = await handle_delegate_tool_call(
            tc,
            session_manager=self.session_manager,
            session=ctx.session,
            agent=ctx.agent,
            agent_registry=_agent_registry,
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
        from cognis.store.queries import get_session_row, list_child_sessions

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
            # Mark as failed in DB + sync to Intaris
            await self.session_manager.mark_failed(
                cancel_id,
                result_summary="Cancelled by parent session",
            )
            return ToolResult(
                output=json.dumps({"status": "cancelled", "session_id": cancel_id}),
            )

        return ToolResult(
            output=json.dumps({"status": "error", "message": f"Unknown tool: {tc.name}"}),
            is_error=True,
        )

    async def _handle_task_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
        events_to_record: list[SessionEvent],
    ) -> ToolResult:
        """Handle task management tools in main-chat contexts."""
        from cognis.core.management import (
            resolve_task_pause_action,
            respond_task_input,
            task_pending_pause_response,
            task_workflow_run_response,
        )
        from cognis.store.queries import (
            get_task,
            list_step_runs_for_task,
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
                from cognis.core.workflow_management import get_workflow_for_user
                from cognis.models.task import TaskDelivery

                # Resolve agent_id: LLMs sometimes pass "self" literally
                # instead of omitting the field.  Fall back to the current
                # agent to avoid FK violations.
                raw_agent_id = tc.arguments.get("agent_id")
                if not raw_agent_id or raw_agent_id == "self":
                    raw_agent_id = ctx.agent.agent_id

                workflow_id = tc.arguments.get("workflow_id")
                if workflow_id:
                    workflow = await get_workflow_for_user(
                        workflow_registry=_workflow_registry_for_agent_loop(self),
                        workflow_id=str(workflow_id),
                        owner_email=ctx.session.user_email,
                    )
                    if workflow is None:
                        return ToolResult(
                            output=json.dumps(
                                {
                                    "status": "error",
                                    "message": "Workflow not found or not accessible.",
                                }
                            ),
                            is_error=True,
                        )

                task = await task_queue.submit(
                    created_by=ctx.session.user_email,
                    agent_id=raw_agent_id,
                    title=tc.arguments.get("title", "Untitled task"),
                    description=tc.arguments.get("description", ""),
                    expected_output=tc.arguments.get("expected_output"),
                    priority=tc.arguments.get("priority", 0),
                    source_type="agent",
                    source_ref=ctx.conversation.conversation_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    workflow_id=workflow_id,
                )

                # Record delegation event so the card appears in session
                # history on page refresh.
                events_to_record.append(
                    SessionEvent(
                        type="delegation",
                        data={
                            "mode": "task",
                            "call_id": tc.call_id,
                            "task": task.title,
                            "child_session_id": task.task_id,
                            "status": "started",
                        },
                    )
                )

                # Publish event for real-time WebSocket delivery so the
                # delegation card appears immediately in the UI.
                await self.event_bus.publish(
                    Event(
                        type=EventType.DELEGATION_STARTED,
                        data={
                            "conversation_id": ctx.conversation.conversation_id,
                            "parent_session_id": ctx.session.session_id,
                            "child_session_id": task.task_id,
                            "mode": "task",
                            "agent_id": task.agent_id,
                            "task": task.title,
                        },
                    )
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
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps(
                            {"status": "error", "message": "Task belongs to a different agent."}
                        ),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
            task_model = _task_row_to_model(task_row)
            step_run_summaries = [
                {
                    "step_name": sr.step_name,
                    "status": sr.status,
                    "attempt": sr.attempt,
                    "summary": (sr.output or {}).get("summary", "") if sr.output else "",
                }
                for sr in step_rows
            ]
            pending_pause = task_pending_pause_response(self.pause_waiter, task_model)
            workflow_run = await task_workflow_run_response(
                task_model,
                workflow_registry=_workflow_registry_for_agent_loop(self),
                pending_pause=pending_pause,
            )
            return ToolResult(
                output=json.dumps(
                    {
                        "task_id": task_row.task_id,
                        "title": task_row.title,
                        "description": task_row.description,
                        "expected_output": task_row.expected_output,
                        "status": task_row.status,
                        "priority": task_row.priority,
                        "workflow_id": task_row.workflow_id,
                        "created_at": str(task_row.created_at) if task_row.created_at else None,
                        "started_at": str(task_row.started_at) if task_row.started_at else None,
                        "completed_at": str(task_row.completed_at)
                        if task_row.completed_at
                        else None,
                        "result_summary": task_row.result_summary,
                        "result_data": task_row.result_data,
                        "pending_pause": (
                            pending_pause.model_dump(mode="json")
                            if pending_pause is not None
                            else None
                        ),
                        "workflow_run": (
                            workflow_run.model_dump(mode="json")
                            if workflow_run is not None
                            else None
                        ),
                        "step_runs": step_run_summaries,
                    },
                    default=str,
                ),
            )

        elif tc.name == "get_task_output":
            task_id = tc.arguments.get("task_id", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
            # Find the last completed step with output
            completed = [sr for sr in reversed(step_rows) if sr.status == "approved" and sr.output]
            if completed:
                output = completed[0].output
                return ToolResult(
                    output=json.dumps(
                        {
                            "step_name": completed[0].step_name,
                            "summary": output.get("summary", ""),
                            "content": output.get("content", ""),
                            "claims": output.get("claims", []),
                            "outputs": output.get("outputs", {}),
                        },
                        default=str,
                    ),
                )
            return ToolResult(
                output=json.dumps(
                    {
                        "summary": task_row.result_summary or "No output available.",
                        "content": "",
                        "claims": [],
                        "outputs": {},
                    }
                ),
            )

        elif tc.name == "get_task_step_output":
            task_id = tc.arguments.get("task_id", "")
            step_name = tc.arguments.get("step_name", "")
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                step_rows = await list_step_runs_for_task(db, task_id)
            matching = [sr for sr in step_rows if sr.step_name == step_name]
            if not matching:
                return ToolResult(
                    output=json.dumps({"error": f"No step '{step_name}' found for this task."}),
                    is_error=True,
                )
            latest = matching[-1]
            output = latest.output or {}
            return ToolResult(
                output=json.dumps(
                    {
                        "step_name": latest.step_name,
                        "status": latest.status,
                        "attempt": latest.attempt,
                        "summary": output.get("summary", ""),
                        "content": output.get("content", ""),
                        "claims": output.get("claims", []),
                        "outputs": output.get("outputs", {}),
                        "error": output.get("error"),
                    },
                    default=str,
                ),
            )

        elif tc.name == "respond_task_input":
            task_id = tc.arguments.get("task_id", "")
            response = str(tc.arguments.get("response", "")).strip()
            if not response:
                return ToolResult(
                    output=json.dumps({"error": "response is required."}),
                    is_error=True,
                )
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
            try:
                result = await respond_task_input(
                    task=_task_row_to_model(task_row),
                    response=response,
                    pause_waiter=self.pause_waiter,
                    notification_service=getattr(self, "notification_service", None),
                    task_queue=self._task_queue,
                    session_factory=self.session_manager.session_factory,
                    user_email=getattr(ctx.session, "user_email", ctx.agent.owner_email),
                )
            except (ValueError, RuntimeError) as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(output=json.dumps({"task_id": task_id, **result}))

        elif tc.name in {"retry_task", "resolve_task_pause"}:
            task_id = tc.arguments.get("task_id", "")
            task_queue = self._task_queue
            if task_queue is None:
                return ToolResult(
                    output=json.dumps({"error": "Task queue is not available."}), is_error=True
                )
            requested_action = (
                "retry" if tc.name == "retry_task" else str(tc.arguments.get("action", "retry"))
            )
            note = _extract_operator_note(tc.arguments)
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"error": "Task not found."}), is_error=True
                    )
                if not await self._can_access_task(task_row, ctx):
                    return ToolResult(
                        output=json.dumps({"error": "Task belongs to a different agent."}),
                        is_error=True,
                    )
                if task_row.status not in ("failed", "paused"):
                    return ToolResult(
                        output=json.dumps(
                            {
                                "error": f"Cannot retry task in '{task_row.status}' status. Only failed or paused tasks can be retried."
                            }
                        ),
                        is_error=True,
                    )
            try:
                result = await resolve_task_pause_action(
                    task=_task_row_to_model(task_row),
                    requested_action=requested_action,
                    note=note,
                    pause_waiter=self.pause_waiter,
                    notification_service=getattr(self, "notification_service", None),
                    task_queue=self._task_queue,
                    session_factory=self.session_manager.session_factory,
                    user_email=getattr(ctx.session, "user_email", ctx.agent.owner_email),
                )
                return ToolResult(output=json.dumps({"task_id": task_id, **result}))
            except ValueError as exc:
                pauses = self.pause_waiter.list_pending(task_id=task_id)
                if pauses and pauses[0].pause_type in ("step_question", "step_input"):
                    question = pauses[0].question or "No question text available"
                    return ToolResult(
                        output=json.dumps(
                            {
                                "error": (
                                    f"Task is waiting for input on step '{pauses[0].step_name or 'unknown'}'. "
                                    f"Question: {question}. Use respond_task_input to continue."
                                )
                            }
                        ),
                        is_error=True,
                    )
                if pauses and pauses[0].pause_type == "escalation":
                    tool_name = (
                        (pauses[0].context or {}).get("tool_name", "a tool call")
                        if pauses[0].context
                        else "a tool call"
                    )
                    return ToolResult(
                        output=json.dumps(
                            {
                                "error": (
                                    f"Task is waiting for escalation approval on {tool_name}. "
                                    "Use /approve or /deny to resolve the escalation first."
                                )
                            }
                        ),
                        is_error=True,
                    )
                if (
                    not pauses
                    and task_row.status in ("failed", "paused")
                    and requested_action == "retry"
                ):
                    await task_queue.retry_failed_task(task_id)
                    return ToolResult(
                        output=json.dumps(
                            {
                                "status": "retrying",
                                "task_id": task_id,
                                "message": "Task reset and relaunched for retry.",
                            }
                        ),
                    )
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            except RuntimeError as exc:
                return ToolResult(
                    output=json.dumps({"error": f"Failed to retry task: {exc}"}), is_error=True
                )

        elif tc.name == "update_task":
            task_id = tc.arguments.get("task_id", "")
            from cognis.core.workflow_management import get_workflow_for_user

            workflow_id = tc.arguments.get("workflow_id")
            if workflow_id:
                workflow = await get_workflow_for_user(
                    workflow_registry=_workflow_registry_for_agent_loop(self),
                    workflow_id=str(workflow_id),
                    owner_email=ctx.session.user_email,
                )
                if workflow is None:
                    return ToolResult(
                        output=json.dumps(
                            {"status": "error", "message": "Workflow not found or not accessible."}
                        ),
                        is_error=True,
                    )
            async with self.session_manager.session_factory() as db:
                task_row = await get_task(db, task_id)
                if task_row is None:
                    return ToolResult(
                        output=json.dumps({"status": "error", "message": "Task not found."}),
                        is_error=True,
                    )
                if not await self._can_access_task(task_row, ctx):
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
            if not await self._can_access_task(task_row, ctx):
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

    async def _can_access_task(self, task_row: Any, ctx: StepContext) -> bool:
        task_owner = getattr(task_row, "created_by", None)
        current_user = getattr(ctx.session, "user_email", ctx.agent.owner_email)
        if not task_owner or task_owner != current_user:
            return False

        task_agent_id = getattr(task_row, "agent_id", None)
        current_agent_id = ctx.agent.agent_id
        if not task_agent_id:
            return False
        if task_agent_id == current_agent_id:
            return True

        session_factory = getattr(self.session_manager, "session_factory", None)
        if session_factory is None:
            return False

        from cognis.core.agent_registry import AgentRegistry

        registry = AgentRegistry(session_factory)
        task_agent = await registry.get(task_agent_id)
        current_agent = await registry.get(current_agent_id)
        task_agent_type = task_agent.agent_type if task_agent is not None else "primary"
        current_agent_type = current_agent.agent_type if current_agent is not None else "primary"

        if task_agent_type == "primary" and current_agent_type == "secondary":
            return await registry.is_secondary_bound(task_agent_id, current_agent_id)
        if task_agent_type == "secondary" and current_agent_type == "primary":
            return await registry.is_secondary_bound(current_agent_id, task_agent_id)
        return False

    async def _handle_workflow_tool(
        self,
        tc: ToolCall,
        *,
        ctx: StepContext,
    ) -> ToolResult:
        """Handle workflow CRUD tools in main-chat contexts."""

        from cognis.api.serializers import workflow_to_response
        from cognis.core.management import workflow_row_to_summary
        from cognis.core.workflow_management import (
            create_user_workflow,
            delete_user_workflow,
            duplicate_visible_workflow,
            get_workflow_for_user,
            list_workflows_for_user,
            update_user_workflow,
        )

        workflow_registry = _workflow_registry_for_agent_loop(self)
        owner_email = ctx.session.user_email

        if tc.name == "list_workflows":
            workflows = await list_workflows_for_user(
                workflow_registry=workflow_registry,
                owner_email=owner_email,
            )
            items = [workflow_row_to_summary(workflow) for workflow in workflows]
            return ToolResult(output=json.dumps({"workflows": items, "count": len(items)}))

        if tc.name == "get_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            workflow = await get_workflow_for_user(
                workflow_registry=workflow_registry,
                workflow_id=workflow_id,
                owner_email=owner_email,
            )
            if workflow is None:
                return ToolResult(
                    output=json.dumps({"error": "Workflow not found."}), is_error=True
                )
            return ToolResult(
                output=json.dumps(workflow_to_response(workflow).model_dump(mode="json"))
            )

        if tc.name == "create_workflow":
            try:
                row = await create_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    owner_email=owner_email,
                    payload=tc.arguments,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "created",
                        "workflow": workflow_to_response(row).model_dump(mode="json"),
                    }
                )
            )

        if tc.name == "update_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            try:
                row = await update_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    workflow_id=workflow_id,
                    owner_email=owner_email,
                    payload={
                        key: value for key, value in tc.arguments.items() if key != "workflow_id"
                    },
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "updated",
                        "workflow": workflow_to_response(row).model_dump(mode="json"),
                    }
                )
            )

        if tc.name == "delete_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            try:
                ok = await delete_user_workflow(
                    session_factory=self.session_manager.session_factory,
                    workflow_id=workflow_id,
                    owner_email=owner_email,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps({"status": "deleted", "workflow_id": workflow_id, "ok": ok})
            )

        if tc.name == "duplicate_workflow":
            workflow_id = str(tc.arguments.get("workflow_id", "")).strip()
            try:
                row = await duplicate_visible_workflow(
                    session_factory=self.session_manager.session_factory,
                    workflow_registry=workflow_registry,
                    workflow_id=workflow_id,
                    owner_email=owner_email,
                )
            except ValueError as exc:
                return ToolResult(output=json.dumps({"error": str(exc)}), is_error=True)
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "duplicated",
                        "workflow": workflow_to_response(row).model_dump(mode="json"),
                    }
                )
            )

        return ToolResult(
            output=json.dumps({"error": f"Unknown workflow tool: {tc.name}"}),
            is_error=True,
        )

    async def _emergency_flush_events(
        self,
        ctx: StepContext,
        events: list[SessionEvent] | None,
    ) -> None:
        """Best-effort flush of accumulated events on exception paths.

        Called from ``run_step``'s exception handlers to ensure tool call
        history is persisted to Intaris even when the step fails mid-
        execution.  Errors are caught and logged — this method NEVER
        raises, so it cannot mask the original exception.
        """
        if not events:
            return
        pending_count = len(events)
        try:
            if await self._record_events_strict(ctx, events, reason="emergency_flush"):
                logger.info(
                    "agent: emergency flush persisted events",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "event_count": pending_count,
                        }
                    },
                )
        except Exception:
            logger.warning(
                "agent: emergency flush failed — %d events lost",
                len(events),
                extra={
                    "extra_data": {
                        "session_id": ctx.session.session_id,
                        "lost_event_types": [e.type for e in events[:10]],
                    }
                },
                exc_info=True,
            )
        finally:
            self._pending_events = None
            self._pending_events_ctx = None

    @staticmethod
    def _intaris_batch_idempotency_key(
        session_id: str,
        events: list[SessionEvent],
        *,
        reason: str,
    ) -> str:
        payload = json.dumps(
            [{"type": event.type, "data": event.data} for event in events],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]  # noqa: S324
        return f"{session_id}:{reason}:{digest}"

    async def _wait_for_intaris_recovery(
        self,
        ctx: StepContext,
        *,
        operation: str,
        on_token: TokenCallback | None = None,
    ) -> None:
        notified = False
        while True:
            self._raise_if_cancelled(ctx)
            try:
                health = await self.providers.guardrails.health()
            except Exception:
                health = None
            if health is not None and health.status == "healthy":
                return
            if not notified:
                logger.warning(
                    "agent: pausing for Intaris recovery",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "operation": operation,
                        }
                    },
                )
                if on_token is not None:
                    await on_token("\n\n[Paused waiting for Intaris to recover.]\n\n")
                notified = True
            if ctx.cancel_event is None:
                await asyncio.sleep(_INTARIS_RETRY_POLL_SECONDS)
                continue
            try:
                await asyncio.wait_for(ctx.cancel_event.wait(), timeout=_INTARIS_RETRY_POLL_SECONDS)
            except TimeoutError:
                continue
            self._raise_if_cancelled(ctx)

    async def _record_events_strict(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str,
        on_token: TokenCallback | None = None,
    ) -> bool:
        if not events:
            return False
        batch = list(events)
        intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
        idempotency_key = self._intaris_batch_idempotency_key(intaris_id, batch, reason=reason)
        while True:
            self._raise_if_cancelled(ctx)
            try:
                append_result = await self.providers.guardrails.record_events(
                    session_id=intaris_id,
                    events=batch,
                    source="cognis",
                    idempotency_key=idempotency_key,
                )
                if not append_result.ok:
                    raise RuntimeError(f"Intaris did not persist {reason}")
                await self.session_cache.append_recorded_events(ctx.session, batch, append_result)
                events.clear()
                return True
            except Exception as exc:
                if not is_retryable_http_error(exc):
                    raise
                logger.warning(
                    "agent: Intaris write failed, waiting to retry",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "operation": reason,
                            "error_type": type(exc).__name__,
                        }
                    },
                    exc_info=True,
                )
                await self._wait_for_intaris_recovery(ctx, operation=reason, on_token=on_token)

    async def _report_reasoning_strict(
        self,
        ctx: StepContext,
        *,
        on_token: TokenCallback | None = None,
    ) -> Any:
        intaris_id = ctx.session.intaris_session_id or ctx.session.session_id
        while True:
            self._raise_if_cancelled(ctx)
            try:
                return await self.providers.guardrails.report_reasoning(
                    session_id=intaris_id,
                    from_events=True,
                    wait_for_intention=ctx.bootstrap_wait_for_intention,
                    wait_timeout_ms=_BOOTSTRAP_INTENTION_WAIT_MS,
                )
            except Exception as exc:
                if not is_retryable_http_error(exc):
                    raise
                logger.warning(
                    "agent: Intaris reasoning failed, waiting to retry",
                    extra={
                        "extra_data": {
                            "session_id": ctx.session.session_id,
                            "error_type": type(exc).__name__,
                        }
                    },
                    exc_info=True,
                )
                await self._wait_for_intaris_recovery(
                    ctx,
                    operation="report_reasoning",
                    on_token=on_token,
                )

    @staticmethod
    def _get_incomplete_todos(ctx: StepContext) -> list[dict[str, Any]]:
        """Return todos that are not completed or cancelled."""
        return [
            todo
            for todo in _normalize_todos(ctx.todos)
            if todo.get("status") not in ("completed", "cancelled")
        ]

    async def _flush_events_incremental(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        reason: str = "incremental",
        on_token: TokenCallback | None = None,
    ) -> None:
        """Flush accumulated events to Intaris immediately."""
        if not events:
            return
        await self._record_events_strict(ctx, events, reason=reason, on_token=on_token)

    async def _finalize_step(
        self,
        ctx: StepContext,
        events: list[SessionEvent],
        *,
        assistant_content_parts: list[str] | None = None,
        assistant_memory_parts: list[str] | None = None,
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
            await self._dispatch_remember(ctx, assistant_memory_parts)
            return True

        events_recorded = await self._record_events_strict(ctx, events, reason="finalize")

        # Dispatch remember — use assistant_memory_parts if provided
        # (covers incrementally-flushed events), fall back to extracting
        # from the events list (covers the non-incremental path).
        if assistant_memory_parts is None:
            extracted = [
                merge_content_and_attachment_note(
                    str(e.data.get("content", "")),
                    [a for a in e.data.get("attachments", []) if isinstance(a, dict)],
                )
                for e in events
                if e.type == "assistant_message"
                and (e.data.get("content") or e.data.get("attachments"))
            ]
            await self._dispatch_remember(ctx, extracted or None)
        else:
            await self._dispatch_remember(ctx, assistant_memory_parts)

        return events_recorded

    async def _dispatch_remember(
        self,
        ctx: StepContext,
        content_parts: list[str] | None,
    ) -> None:
        """Enqueue last turn (user + assistant) to Mnemory remember queue.

        Sends both the user message and assistant response so mnemory can
        extract facts from the full exchange — matching the pattern used by
        the OpenWebUI and OpenClaw mnemory integrations.
        """
        if not content_parts:
            return
        if not ctx.session.mnemory_session_id:
            logger.warning(
                "agent: skipping remember — no mnemory_session_id",
                extra={"extra_data": {"session_id": ctx.session.session_id}},
            )
            return
        assistant_content = " ".join(content_parts)
        if not assistant_content.strip():
            return

        # Build the last turn: user message + assistant response.
        # Mnemory's remember endpoint expects OpenAI-format messages
        # and extracts facts from both roles.  System-initiated turns
        # (workflow steps, delegations) use internal prompts as
        # user_message — skip those to avoid polluting user memory.
        messages: list[dict[str, str]] = []
        if not ctx.system_initiated and ctx.user_message and ctx.user_message.strip():
            messages.append({"role": "user", "content": ctx.user_message[:5000]})
        messages.append({"role": "assistant", "content": assistant_content[:5000]})

        await self.remember_queue.enqueue(
            {
                "session_id": ctx.session.mnemory_session_id,
                "messages": messages,
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

        Includes task context, prior step outputs (resolved from the step's
        input configuration), the step objective, and any in-progress todos.
        Prior step outputs are included directly in the prompt so they are
        visible in session logs and prominent to the LLM.
        """
        parts: list[str] = []

        # Inject task context so the LLM knows what the workflow is about
        if ctx.task_title or ctx.task_description:
            parts.append("## Task\n\n")
            if ctx.task_title:
                parts.append(f"**{ctx.task_title}**\n\n")
            if ctx.task_description:
                parts.append(f"{ctx.task_description}\n\n")
            if ctx.task_expected_output:
                parts.append(f"**Expected output:** {ctx.task_expected_output}\n\n")

        # Inject prior step outputs so the LLM has context from previous steps.
        # This resolves the step's input configuration and reads structured
        # outputs from workflow state, making them visible in session logs.
        prior_output_text = self._format_prior_step_outputs(ctx)
        if prior_output_text:
            parts.append(f"## Prior Step Output\n\n{prior_output_text}\n\n")

        prompt_text = ctx.user_message or ctx.step_definition.prompt
        parts.append(f"## Step: {ctx.step_definition.name}\n\n{prompt_text}")

        if ctx.todos:
            parts.append("\n\n## Your step todos:\n")
            for todo in ctx.todos:
                status = todo.get("status", "pending")
                content = todo.get("content", "")
                parts.append(f"- [{status}] {content}")

        feedback = getattr(ctx.workflow_state, "last_evaluation_feedback", None)
        if feedback:
            parts.append(f"\n\n## Revision Feedback\n\n{feedback}")

        revision_context = getattr(ctx.workflow_state, "last_revision_context", None)
        if revision_context:
            parts.append(f"\n\n## Revision Context\n\n{revision_context}")
            ctx.workflow_state.last_revision_context = None

        operator_instruction = getattr(ctx.workflow_state, "last_operator_instruction", None)
        if operator_instruction:
            parts.append(
                "\n\n## Operator Instruction\n\n"
                "A human explicitly chose to continue or retry the workflow with this "
                f"instruction:\n\n{operator_instruction}"
            )

        parts.append(
            "\n\n---\n"
            "When you have completed the objective, write out your findings and "
            "deliverables as a detailed text response. Then call step_complete "
            "with a summary, structured outputs, verifiable claims, and an "
            "outcome when the completed step should explicitly report rejection "
            "or failure."
        )

        return "".join(parts)

    def _format_prior_step_outputs(self, ctx: StepContext) -> str:
        """Format prior step outputs for inclusion in the step prompt.

        Resolves the step's input configuration and reads structured outputs
        from ``workflow_state.step_outputs``.  Returns empty string if no
        prior outputs are available (first step or null input).
        """
        from cognis.models.workflow import StepOutput, resolve_effective_input

        if not ctx.workflow_state or not ctx.workflow_steps:
            return ""

        effective_input = resolve_effective_input(
            ctx.step_definition, ctx.step_index, ctx.workflow_steps
        )
        if effective_input.type == "null":
            return ""

        source_names = effective_input.source_names()
        if not source_names:
            return ""

        sections: list[str] = []
        for source_name in source_names:
            raw = ctx.workflow_state.step_outputs.get(source_name)
            if raw is None:
                continue
            output = StepOutput.model_validate(raw)
            section_parts = [f'<step_output source="{source_name}">']
            if effective_input.type == "full":
                if output.summary:
                    section_parts.append(f"Summary: {output.summary}")
                if output.claims:
                    claims_str = "\n".join(f"  - {c}" for c in output.claims)
                    section_parts.append(f"Claims:\n{claims_str}")
                if output.content:
                    section_parts.append(f"Content:\n{output.content}")
                if output.outputs:
                    section_parts.append(
                        f"Structured outputs:\n{json.dumps(output.outputs, indent=2, default=str)}"
                    )
            elif effective_input.type == "summary":
                if output.summary:
                    section_parts.append(f"Summary: {output.summary}")
                if output.outputs:
                    section_parts.append(
                        f"Structured outputs:\n{json.dumps(output.outputs, indent=2, default=str)}"
                    )
            else:
                if output.summary:
                    section_parts.append(f"Summary: {output.summary}")
                if output.claims:
                    claims_str = "\n".join(f"  - {c}" for c in output.claims)
                    section_parts.append(f"Claims:\n{claims_str}")
                if output.outputs:
                    section_parts.append(
                        f"Structured outputs:\n{json.dumps(output.outputs, indent=2, default=str)}"
                    )
            section_parts.append("</step_output>")
            sections.append("\n".join(section_parts))

        return "\n\n".join(sections)

    def _build_controller_tool_schemas(self, ctx: StepContext) -> list[dict[str, Any]]:
        """Build JSON schemas for controller-injected tools."""
        from cognis.tools.builtin.orchestration import orchestration_tools

        tools: list[dict[str, Any]] = []

        # step_complete — available when policy allows it (workflow steps, sub-sessions)
        if ctx.policy.step_complete_available:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": STEP_COMPLETE,
                        "description": (
                            "Signal that this step is complete. Before calling this "
                            "tool, you MUST write out your findings, deliverables, or "
                            "results as a text message — the evaluator verifies your "
                            "work against your written output. Then call this tool. "
                            "All todos must be completed or cancelled before calling this."
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
                                    "description": (
                                        "Key structured data produced by this step "
                                        "(e.g., URLs found, metrics gathered, file paths "
                                        "modified). Include data that downstream steps or "
                                        "the evaluator need."
                                    ),
                                },
                                "claims": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Specific, verifiable claims about what was "
                                        "accomplished. Each claim should be independently "
                                        "checkable against your written output above."
                                    ),
                                },
                                "outcome": {
                                    "type": "object",
                                    "description": (
                                        "Optional business outcome for this step. Use "
                                        "'rejected' when the step completed properly but "
                                        "the reviewed work should go back for revision, or "
                                        "'failed' when the step itself could not complete "
                                        "successfully."
                                    ),
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "enum": ["success", "rejected", "failed"],
                                        },
                                        "reason": {
                                            "type": "string",
                                            "description": "Required for rejected or failed outcomes.",
                                        },
                                    },
                                    "required": ["status"],
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
                        "description": (
                            "Request input from the caller while staying in the same "
                            "step or direct-chat turn. Use this when you are blocked on "
                            "missing information and need the answer before continuing."
                        ),
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
                            "Track progress within this execution context. Use todos for "
                            "complex work with multiple concrete actions, not for trivial "
                            "single-step work or high-level plans. Break substantial work "
                            "into specific, actionable items instead of 2-3 broad buckets. "
                            "Prefer a fuller checklist when the task spans inspection, "
                            "implementation, tests, docs, and verification. Keep exactly "
                            "one todo in status 'in_progress' at a time, mark items "
                            "'completed' immediately after finishing them, and use "
                            "'cancelled' only for work that no longer applies. In chat, "
                            "todos should cover only concrete work you are actively "
                            "continuing in the current turn. In workflow steps and "
                            "delegated sub-sessions, create or update todos before "
                            "substantial work and keep them accurate until the step is done."
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
                                                    "completed",
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
                        "description": "Read current execution-context todos.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ]
        )

        if _controller_builtin_enabled(ctx.agent, SEARCH_TOOLS_TOOL):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": SEARCH_TOOLS_TOOL.name,
                        "description": SEARCH_TOOLS_TOOL.description,
                        "parameters": SEARCH_TOOLS_TOOL.parameters,
                    },
                }
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

    def _get_initial_discovered_tool_ids(self, ctx: StepContext) -> set[str]:
        """Return tool ids that should be visible before any discovery calls."""

        if not isinstance(ctx.agent.skills, dict):
            return set()
        raw_ids = ctx.agent.skills.get("_attached_skill_tool_ids")
        if not isinstance(raw_ids, list):
            return set()
        return {str(tool_id) for tool_id in raw_ids if isinstance(tool_id, str) and tool_id.strip()}

    def _merge_discovered_tool_ids(
        self, discovered_tool_ids: set[str], metadata: dict[str, Any]
    ) -> None:
        """Merge newly discovered tool ids from tool metadata."""

        raw_ids = metadata.get("discovered_tool_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []
        discovered_tool_ids.update(
            str(tool_id) for tool_id in raw_ids if isinstance(tool_id, str) and tool_id.strip()
        )
        removed_ids = metadata.get("removed_tool_ids")
        if isinstance(removed_ids, list):
            discovered_tool_ids.difference_update(
                str(tool_id)
                for tool_id in removed_ids
                if isinstance(tool_id, str) and tool_id.strip()
            )

    def _apply_skill_attachment_metadata(self, ctx: StepContext, metadata: dict[str, Any]) -> None:
        """Keep in-memory agent skill refs aligned with skill management mutations."""

        if not isinstance(ctx.agent.skills, dict):
            ctx.agent.skills = {}

        items = ctx.agent.skills.get("items")
        if not isinstance(items, list):
            items = []

        attached_skill_id = metadata.get("attached_skill_id")
        if (
            isinstance(attached_skill_id, str)
            and attached_skill_id.strip()
            and not any(
                isinstance(item, dict) and item.get("skill_id") == attached_skill_id
                for item in items
            )
        ):
            items.append({"skill_id": attached_skill_id, "enabled": True})

        deleted_skill_id = metadata.get("deleted_skill_id")
        if isinstance(deleted_skill_id, str) and deleted_skill_id.strip():
            items = [
                item
                for item in items
                if not (isinstance(item, dict) and item.get("skill_id") == deleted_skill_id)
            ]
            attached_tool_ids = _attached_skill_tool_ids(ctx.agent)
            ctx.agent.skills["_attached_skill_tool_ids"] = [
                tool_id
                for tool_id in attached_tool_ids
                if not tool_id.startswith(f"skill:{deleted_skill_id}:")
            ]

        ctx.agent.skills["items"] = items

    def _get_executor(self, ctx: StepContext) -> Any:
        """Get the executor connection for the current step."""
        if ctx.executor_connection is not None:
            return ctx.executor_connection
        return getattr(self.providers, "_executor_connection", None)
