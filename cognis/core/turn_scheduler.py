"""Transport-agnostic turn orchestration.

TurnScheduler owns the full lifecycle of a chat turn — from user message
to response — without any dependency on WebSocket or other transport layers.

It handles:
- Turn submission and serialization (one active turn per conversation)
- Decision engine dispatch for inline chat turns
- Follow-up turns (system-initiated, via EventBus subscription)
- Turn cancellation
- Error classification
- Post-turn housekeeping (last_message_at, session cache refresh, title change)
- Conversation runtime loading (including deferred session creation after compaction)

Transport layers (WebSocket, REST, channel adapters) use TurnScheduler
as their single entry point for chat turns and register TurnObserver
instances for real-time streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import inspect
import json
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from time import monotonic
from typing import Any, Protocol, runtime_checkable

import httpx
from prometheus_client import Counter, Histogram
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.core.agent_direct import is_agent_direct_context
from cognis.core.attachment_utils import (
    attachment_placeholder_text,
    normalize_attachment_refs,
    strip_attachment_payload_bytes,
)
from cognis.core.chat_modes import (
    ChatMode,
    ResolvedChatMode,
    parse_chat_mode_directive,
    resolve_chat_mode,
)
from cognis.core.commands import is_system_slash_command_message
from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import (
    ContinuationFollowUp,
    FollowUpMetadata,
    FollowUpMode,
    FollowUpOriginKind,
    FollowUpRelevanceHint,
    FollowUpRequiredAction,
    FollowUpStatus,
    build_follow_up_id,
    parse_follow_up_metadata,
    render_follow_up_turn_notice,
    truncate_follow_up_text,
)
from cognis.core.long_lived_chat import is_long_lived_chat_context
from cognis.core.title_policy import can_adopt_intaris_title, sync_intaris_title
from cognis.core.tool_output_presentation import build_transport_tool_output_preview
from cognis.core.tool_output_spool import ToolOutputSpool, ToolOutputSpoolPage
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.session import (
    BLOCKED_STATES,
    ConversationModel,
    SessionEvent,
    SessionModel,
    SessionStatus,
)
from cognis.models.task import TaskDelivery
from cognis.runtime_context import (
    current_agent_id,
    current_agent_owner_email,
    current_effective_working_directory,
    current_user_email,
    current_workspace_root,
)
from cognis.store import queries
from cognis.store.models import FollowUpDedupeRow
from cognis.store.queries import get_setting_value

logger = get_logger(__name__)

_CANCELLED_TURN_ERROR_CODES = {"cancelled", "turn_cancelled"}

_MAX_ACTIVE_TOOL_OUTPUT_CHARS = 64_000
_ACTIVE_TOOL_OUTPUT_SNAPSHOT_TTL_SECONDS = 6 * 60 * 60

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

TURNS_TOTAL = Counter(
    "cognis_turns_total",
    "Total chat turns executed",
    ["outcome"],  # completed, delegated, error, cancelled
)
TURN_DURATION = Histogram(
    "cognis_turn_duration_seconds",
    "Duration of chat turns",
    ["type"],  # user, system
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
FOLLOW_UP_DEDUPE_TOTAL = Counter(
    "cognis_follow_up_dedupe_total",
    "Suppressed duplicate follow-up turn requests",
    ["reason"],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ACTIVE_TURNS_PER_USER = 20
DEFAULT_MAX_QUEUED_MESSAGES = 20
DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS = 21600
DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS = 20
_MAX_DEFERRED_LOCKS = 200
FOLLOW_UP_DEDUPE_TTL_SECONDS = 600.0
MAX_AUTOMATIC_CONTINUATION_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _positive_int_setting(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int_setting(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _is_expired_timestamp(value: datetime | None, *, now: datetime | None = None) -> bool:
    normalized = _normalize_utc(value)
    if normalized is None:
        return True
    return normalized <= (now or _utcnow())


def _effective_user_content(content: str, attachments: list[AttachmentRef]) -> str:
    if content.strip():
        return content
    if not attachments:
        return content
    return attachment_placeholder_text(attachment.kind for attachment in attachments)


def _should_bootstrap_wait_for_intention(conversation: ConversationModel) -> bool:
    """Return whether the first model turn should wait for Intaris intention bootstrap."""

    if can_adopt_intaris_title(conversation):
        return True
    context = conversation.context
    if context is None:
        return False
    if not is_agent_direct_context(context.type, context.platform_data):
        return False
    return not (conversation.title or "").strip()


def _utf16_code_units(value: str) -> int:
    """Return the string length in JavaScript-compatible UTF-16 code units."""

    return len(value.encode("utf-16-le")) // 2


def _model_copy_or_self(value: Any) -> Any:
    """Return a defensive model copy when the runtime object supports it."""

    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        return model_copy(deep=True)
    return value


def _trim_callback_args(callback: Any, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Drop newly added optional callback args for older observers."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return args
    if any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    ):
        return args
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return args[: len(positional)]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TurnError:
    """Structured error from a failed turn."""

    code: str
    message: str
    recoverable: bool
    detail: dict[str, Any] | None = None


@dataclass(slots=True)
class TurnResult:
    """Result of a completed turn."""

    conversation_id: str
    session_id: str
    message_id: str
    turn_id: str | None = None
    last_seq: int = 0
    context_usage: dict[str, Any] | None = None
    delegated: bool = False
    task_id: str | None = None
    error: TurnError | None = None
    title_changed: bool = False
    new_title: str | None = None
    final_content: str | None = None
    system_initiated: bool = False
    channel_deliverable: bool = False
    delivery_id: str | None = None
    delivery_fallback_text: str | None = None
    attachments: list[dict[str, Any]] | None = None
    completed_at: datetime | None = None
    chat_mode: ChatMode = "default"
    chat_mode_source: str = "system_default"
    partial: bool = False
    finish_reason: str | None = None
    managed_continuation_pending: bool = False


@dataclass(slots=True)
class ActiveStreamState:
    """Volatile snapshot of the currently unpersisted assistant stream."""

    conversation_id: str
    session_id: str
    message_id: str
    turn_id: str | None
    content: str = ""
    chunk_count: int = 0
    updated_at: datetime = field(default_factory=_utcnow)

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "content": self.content,
            "chunk_count": self.chunk_count,
            "content_offset": _utf16_code_units(self.content),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(slots=True)
class ActiveToolOutputSnapshot:
    """Volatile bounded snapshot of streamed tool output."""

    conversation_id: str
    session_id: str
    call_id: str
    tool_name: str
    turn_id: str | None
    status: str = "running"
    result: str = ""
    stream: str | None = None
    is_error: bool = False
    chunk_count: int = 0
    content_offset: int = 0
    output_size: int = 0
    truncated: bool = False
    agent_visible_truncated: bool = False
    transport_truncated: bool = False
    has_full_output: bool = False
    recovery_call_id: str | None = None
    tool_output_artifact_id: str | None = None
    anchors_available: bool = False
    anchor_count: int = 0
    updated_at: datetime = field(default_factory=_utcnow)

    def expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) - self.updated_at > timedelta(
            seconds=_ACTIVE_TOOL_OUTPUT_SNAPSHOT_TTL_SECONDS
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "status": self.status,
            "result": self.result,
            "stream": self.stream,
            "is_error": self.is_error,
            "chunk_count": self.chunk_count,
            "content_offset": self.content_offset,
            "output_size": self.output_size,
            "truncated": self.truncated,
            "agent_visible_truncated": self.agent_visible_truncated,
            "transport_truncated": self.transport_truncated,
            "has_full_output": self.has_full_output,
            "recovery_call_id": self.recovery_call_id,
            "tool_output_artifact_id": self.tool_output_artifact_id,
            "anchors_available": self.anchors_available,
            "anchor_count": self.anchor_count,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> ActiveToolOutputSnapshot | None:
        try:
            updated_raw = data.get("updated_at")
            updated_at = (
                datetime.fromisoformat(updated_raw) if isinstance(updated_raw, str) else _utcnow()
            )
            return cls(
                conversation_id=str(data["conversation_id"]),
                session_id=str(data["session_id"]),
                call_id=str(data["call_id"]),
                tool_name=str(data.get("tool_name") or "tool"),
                turn_id=data.get("turn_id") if isinstance(data.get("turn_id"), str) else None,
                status=str(data.get("status") or "running"),
                result=str(data.get("result") or ""),
                stream=data.get("stream") if isinstance(data.get("stream"), str) else None,
                is_error=bool(data.get("is_error")),
                chunk_count=int(data.get("chunk_count") or 0),
                content_offset=int(data.get("content_offset") or 0),
                output_size=int(data.get("output_size") or 0),
                truncated=bool(data.get("truncated")),
                agent_visible_truncated=bool(data.get("agent_visible_truncated")),
                transport_truncated=bool(data.get("transport_truncated")),
                has_full_output=bool(data.get("has_full_output")),
                recovery_call_id=data.get("recovery_call_id")
                if isinstance(data.get("recovery_call_id"), str)
                else None,
                tool_output_artifact_id=data.get("tool_output_artifact_id")
                if isinstance(data.get("tool_output_artifact_id"), str)
                else None,
                anchors_available=bool(data.get("anchors_available")),
                anchor_count=int(data.get("anchor_count") or 0),
                updated_at=updated_at,
            )
        except Exception:
            return None


@dataclass(slots=True)
class _QueuedMessage:
    """A message queued behind an active turn."""

    content: str
    user_email: str
    queue_id: str = field(default_factory=lambda: f"qmsg_{uuid.uuid4().hex}")
    client_message_id: str | None = None
    attachments: list[dict[str, Any]] | None = None
    attachment_notice: str | None = None
    attachment_context: str | None = None
    system_initiated: bool = False
    channel_deliverable: bool = False
    delivery_id: str | None = None
    delivery_fallback_text: str | None = None
    follow_up: FollowUpMetadata | None = None
    outbound_attachments: list[dict[str, Any]] | None = None
    turn_observers: tuple[TurnObserver, ...] = ()
    one_shot_chat_mode: ChatMode | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def snapshot(self, position: int) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "client_message_id": self.client_message_id,
            "content": self.content,
            "attachments": strip_attachment_payload_bytes(self.attachments or []),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "position": position,
        }


@dataclass(slots=True)
class _TurnControl:
    """Mutable state for one active conversation turn."""

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    settled: bool = False
    turn_id: str | None = None
    chat_mode: ChatMode = "default"
    chat_mode_source: str = "system_default"
    turn_observers: list[TurnObserver] = field(default_factory=list)
    absorbed_follow_up_ids: set[str] = field(default_factory=set)
    absorbed_outbound_attachments: list[dict[str, Any]] = field(default_factory=list)
    absorbed_channel_deliverable: bool = False
    absorbed_delivery_id: str | None = None
    absorbed_delivery_fallback_text: str | None = None


def _user_message_event_id(
    *,
    conversation_id: str,
    session_id: str | None,
    turn_id: str | None,
    client_message_id: str | None,
    queue_id: str | None,
    content: str,
) -> str:
    """Build a stable client-visible id for live user message events."""

    if client_message_id:
        return f"client:{client_message_id}"
    if queue_id:
        return f"queue:{queue_id}"
    digest = hashlib.sha256(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "content": content,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"user:{session_id or conversation_id}:{turn_id or digest}:{digest}"


def _user_message_event_payload(
    *,
    conversation_id: str,
    session_id: str | None,
    content: str,
    attachments: list[dict[str, Any]],
    turn_id: str | None = None,
    chat_mode: str | None = None,
    chat_mode_source: str | None = None,
    client_message_id: str | None = None,
    queue_id: str | None = None,
) -> dict[str, Any]:
    event_id = _user_message_event_id(
        conversation_id=conversation_id,
        session_id=session_id,
        turn_id=turn_id,
        client_message_id=client_message_id,
        queue_id=queue_id,
        content=content,
    )
    return {
        "conversation_id": conversation_id,
        "session_id": session_id,
        "content": content,
        "attachments": attachments,
        "turn_id": turn_id,
        "chat_mode": chat_mode,
        "chat_mode_source": chat_mode_source,
        "client_message_id": client_message_id,
        "queue_id": queue_id,
        "event_id": event_id,
        "message_id": event_id,
    }


def _tool_call_ceiling_metadata(step_output: Any | None) -> dict[str, Any] | None:
    metadata = getattr(step_output, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("continuation_reason") != "tool_call_ceiling_reached":
        return None
    return metadata


def _turn_error_from_step_output(step_output: Any | None) -> TurnError | None:
    error_text = str(getattr(step_output, "error", "") or "").strip()
    if not error_text:
        return None
    summary = str(getattr(step_output, "summary", "") or "").strip()
    message = summary or error_text
    return TurnError(
        code="step_failed",
        message=message[:500],
        recoverable=True,
        detail={"error_detail": error_text[:2000]},
    )


def _pending_todos_from_metadata(metadata: dict[str, Any]) -> list[dict[str, str]]:
    todos = metadata.get("pending_todos")
    if not isinstance(todos, list):
        return []
    pending: list[dict[str, str]] = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        content = str(todo.get("content") or "").strip()
        if not content:
            continue
        status = str(todo.get("status") or "pending").strip() or "pending"
        if status in {"completed", "cancelled"}:
            continue
        pending.append({"content": content, "status": status})
    return pending


def _positive_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class SessionCreationFailedError(Exception):
    """Raised when session creation fails during runtime loading."""


# ---------------------------------------------------------------------------
# TurnObserver protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TurnObserver(Protocol):
    """Optional streaming observer for real-time turn delivery.

    Transport layers (WebSocket, SSE, etc.) implement this protocol
    to receive streaming updates during a turn. The TurnScheduler
    calls these methods as the turn progresses.

    All methods are fire-and-forget — errors are logged but never
    propagate to the turn execution.
    """

    async def on_token(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None: ...

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        turn_id: str | None,
    ) -> None: ...

    async def on_tool_progress(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        progress: dict[str, Any],
        turn_id: str | None = None,
    ) -> None: ...

    async def on_tool_result(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        result: str,
        is_error: bool,
        duration_ms: int | None,
        evaluation: dict[str, Any] | None,
        attachments: list[dict[str, Any]] | None = None,
        file_diffs: list[dict[str, Any]] | None = None,
        turn_id: str | None = None,
        presentation: dict[str, Any] | None = None,
    ) -> None: ...

    async def on_tool_output_chunk(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        delta: str,
        stream: str | None,
        turn_id: str | None = None,
        chunk_index: int | None = None,
        content_offset: int | None = None,
    ) -> None: ...

    async def on_thinking(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
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
    ) -> None: ...

    async def on_turn_complete(self, result: TurnResult) -> None: ...

    async def on_turn_error(self, conversation_id: str, error: TurnError) -> None: ...

    async def on_system_message(
        self,
        conversation_id: str,
        text: str,
        notice_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        turn_id: str | None = None,
    ) -> None: ...

    async def on_queued(self, conversation_id: str, queued_count: int) -> None: ...

    async def on_queued_messages(
        self, conversation_id: str, messages: list[dict[str, Any]]
    ) -> None: ...


# ---------------------------------------------------------------------------
# TurnScheduler
# ---------------------------------------------------------------------------


class TurnScheduler:
    """Transport-agnostic turn orchestration.

    Owns the full lifecycle of a chat turn. Transport layers call
    ``submit_turn()`` and optionally register ``TurnObserver`` instances
    for real-time streaming. Lifecycle events are published to the
    EventBus for non-streaming consumers.
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        workflow_engine: Any,
        decision_engine: Any,
        task_queue: Any,
        session_manager: Any,
        session_cache: Any,
        compaction_strategy: Any,
        agent_loop: Any,
        pause_waiter: Any,
        notification_service: Any,
        providers: Any,
        artifact_store: Any,
        workflow_registry: Any,
        event_bus: EventBus,
        tool_output_spool: ToolOutputSpool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._workflow_engine = workflow_engine
        self._decision_engine = decision_engine
        self._task_queue = task_queue
        self._session_manager = session_manager
        self._session_cache = session_cache
        self._compaction_strategy = compaction_strategy
        self._agent_loop = agent_loop
        self._pause_waiter = pause_waiter
        self._notification_service = notification_service
        self._providers = providers
        self._artifact_store = artifact_store
        self._workflow_registry = workflow_registry
        self._event_bus = event_bus
        self._tool_output_spool = tool_output_spool or ToolOutputSpool()

        # Per-conversation turn serialization
        self._active_turns: dict[str, asyncio.Task[None]] = {}
        self._turn_controls: dict[str, _TurnControl] = {}
        self._turn_sessions: dict[str, str] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._queued_messages: dict[str, deque[_QueuedMessage]] = defaultdict(deque)
        self._escalation_notice_pause_ids: dict[str, str] = {}
        self._pending_follow_ups: set[tuple[str, str]] = set()
        self._handled_follow_ups: dict[tuple[str, str], float] = {}
        self._active_streams: dict[str, ActiveStreamState] = {}
        self._active_streams_lock = asyncio.Lock()
        self._published_title_updates: dict[str, str] = {}
        self._active_tool_outputs: dict[tuple[str, str, str], ActiveToolOutputSnapshot] = {}
        self._active_tool_outputs_lock = asyncio.Lock()
        self._active_tool_output_l2: dict[str, dict[str, Any]] = {}
        self._turn_waiters: dict[str, list[asyncio.Future[TurnResult | TurnError]]] = defaultdict(
            list
        )

        # Per-user concurrent turn limit
        self._user_turn_counts: dict[str, int] = defaultdict(int)

        # Conversation-scoped observers (multiple allowed — e.g. multiple browser tabs)
        self._observers: dict[str, list[TurnObserver]] = defaultdict(list)
        self._observer_failures: dict[tuple[str, int], int] = defaultdict(int)
        self._disabled_observers: set[tuple[str, int]] = set()

        # Per-conversation session creation locks (bootstrap + compaction recovery)
        self._deferred_creation_locks: dict[str, asyncio.Lock] = {}
        self._idle_checkpoint_locks: dict[str, asyncio.Lock] = {}

        # Register for follow-up turn events
        event_bus.subscribe(EventType.FOLLOW_UP_TURN_REQUESTED, self._handle_follow_up_event)
        event_bus.subscribe(EventType.CONVERSATION_UPDATED, self._handle_conversation_updated)
        logger.info("turn_scheduler: registered on EventBus")
        logger.info("turn_scheduler: follow-up dedupe backed by durable store when available")

    def _turn_lock(self, conversation_id: str) -> asyncio.Lock:
        """Return the per-conversation lock protecting active-turn ownership."""

        lock = self._turn_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[conversation_id] = lock
        return lock

    # ------------------------------------------------------------------
    # Observer management
    # ------------------------------------------------------------------

    def add_observer(self, conversation_id: str, observer: TurnObserver) -> None:
        """Register a conversation-scoped streaming observer."""
        self._observers[conversation_id].append(observer)

    def remove_observer(self, conversation_id: str, observer: TurnObserver) -> None:
        """Remove a streaming observer for a conversation."""
        observers = self._observers.get(conversation_id)
        if observers:
            with contextlib.suppress(ValueError):
                observers.remove(observer)
            self._observer_failures.pop((conversation_id, id(observer)), None)
            self._disabled_observers.discard((conversation_id, id(observer)))
            if not observers:
                del self._observers[conversation_id]

    def remove_all_observers(self, observer: TurnObserver) -> None:
        """Remove an observer from all conversations (e.g. on disconnect)."""
        empty_keys: list[str] = []
        for cid, observers in self._observers.items():
            with contextlib.suppress(ValueError):
                observers.remove(observer)
            self._observer_failures.pop((cid, id(observer)), None)
            self._disabled_observers.discard((cid, id(observer)))
            if not observers:
                empty_keys.append(cid)
        for cid in empty_keys:
            del self._observers[cid]

    async def active_stream_snapshots(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return volatile in-flight assistant stream snapshots for a conversation."""

        async with self._active_streams_lock:
            snapshot = self._active_streams.get(conversation_id)
            if snapshot is None or not snapshot.content:
                return []
            return [snapshot.snapshot()]

    async def active_tool_output_snapshots(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return volatile bounded streamed tool-output snapshots."""

        await self._load_active_tool_output_l2(conversation_id)
        control = self._turn_controls.get(conversation_id)
        active_turn_id = (
            control.turn_id
            if control is not None
            and not control.settled
            and self.has_running_turn(conversation_id)
            else None
        )
        should_persist = False
        async with self._active_tool_outputs_lock:
            if active_turn_id is None:
                stale_keys = [key for key in self._active_tool_outputs if key[0] == conversation_id]
                for key in stale_keys:
                    self._active_tool_outputs.pop(key, None)
                should_persist = bool(stale_keys)
                snapshots: list[dict[str, Any]] = []
            else:
                self._prune_active_tool_outputs_locked()
                stale_keys: list[tuple[str, str, str]] = []
                snapshots = []
                for key, snapshot in self._active_tool_outputs.items():
                    cid, _, _ = key
                    if cid != conversation_id:
                        continue
                    if snapshot.turn_id != active_turn_id:
                        stale_keys.append(key)
                        continue
                    if snapshot.status == "running" and snapshot.result and not snapshot.expired():
                        snapshots.append(snapshot.snapshot())
                for key in stale_keys:
                    self._active_tool_outputs.pop(key, None)
                should_persist = bool(stale_keys)
        if should_persist:
            await self._persist_active_tool_output_l2(conversation_id)
        return snapshots

    def _active_tool_output_cache_key(self, conversation_id: str) -> str:
        return f"active_tool_outputs:{conversation_id}"

    async def _load_active_tool_output_l2(self, conversation_id: str) -> None:
        cache = getattr(self._session_cache, "_redis", None)
        if cache is None:
            return
        try:
            raw = await cache.get(self._active_tool_output_cache_key(conversation_id))
        except Exception:
            logger.debug("active tool output L2 read failed", exc_info=True)
            return
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:
            return
        if not isinstance(payload, list):
            return
        async with self._active_tool_outputs_lock:
            for item in payload:
                if not isinstance(item, dict):
                    continue
                snapshot = ActiveToolOutputSnapshot.from_snapshot(item)
                if snapshot is None or snapshot.expired():
                    continue
                self._active_tool_outputs[
                    (snapshot.conversation_id, snapshot.session_id, snapshot.call_id)
                ] = snapshot

    async def _persist_active_tool_output_l2(self, conversation_id: str) -> None:
        cache = getattr(self._session_cache, "_redis", None)
        if cache is None:
            return
        async with self._active_tool_outputs_lock:
            self._prune_active_tool_outputs_locked()
            payload = [
                snapshot.snapshot()
                for (cid, _, _), snapshot in self._active_tool_outputs.items()
                if (
                    cid == conversation_id
                    and snapshot.status == "running"
                    and snapshot.result
                    and not snapshot.expired()
                )
            ]
        try:
            key = self._active_tool_output_cache_key(conversation_id)
            if payload:
                await cache.setex(
                    key, _ACTIVE_TOOL_OUTPUT_SNAPSHOT_TTL_SECONDS, json.dumps(payload)
                )
            else:
                await cache.delete(key)
        except Exception:
            logger.debug("active tool output L2 write failed", exc_info=True)

    def _prune_active_tool_outputs_locked(self) -> None:
        expired = [key for key, snapshot in self._active_tool_outputs.items() if snapshot.expired()]
        for key in expired:
            self._active_tool_outputs.pop(key, None)

    async def _append_active_tool_output_chunk(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        turn_id: str | None,
        delta: str,
        stream: str | None,
    ) -> tuple[int, int]:
        async with self._active_tool_outputs_lock:
            key = (conversation_id, session_id, call_id)
            snapshot = self._active_tool_outputs.get(key)
            if snapshot is None:
                snapshot = ActiveToolOutputSnapshot(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                )
                self._active_tool_outputs[key] = snapshot
            index = snapshot.chunk_count
            offset = snapshot.content_offset
            next_result = snapshot.result + delta
            full_output_size = snapshot.output_size + len(delta)
            next_offset = snapshot.content_offset + _utf16_code_units(delta)
            preview = build_transport_tool_output_preview(
                next_result,
                _MAX_ACTIVE_TOOL_OUTPUT_CHARS,
                metadata={"output_size": full_output_size},
            )
            snapshot.result = preview.result
            snapshot.truncated = preview.truncated
            snapshot.transport_truncated = snapshot.truncated
            snapshot.stream = stream
            snapshot.is_error = snapshot.is_error or stream == "stderr"
            snapshot.chunk_count += 1
            snapshot.content_offset = next_offset
            snapshot.output_size = full_output_size
            snapshot.updated_at = _utcnow()
        self._tool_output_spool.append(
            conversation_id=conversation_id,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            turn_id=turn_id,
            text=delta,
            stream=stream,
        )
        await self._persist_active_tool_output_l2(conversation_id)
        return index, offset

    async def _finalize_active_tool_output(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        turn_id: str | None,
        result: str,
        is_error: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._active_tool_outputs_lock:
            key = (conversation_id, session_id, call_id)
            snapshot = self._active_tool_outputs.get(key)
            if snapshot is None:
                snapshot = ActiveToolOutputSnapshot(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                )
                self._active_tool_outputs[key] = snapshot
            meta = metadata or {}
            snapshot.status = "failed" if is_error else "completed"
            if not (meta.get("transport_truncated") and len(snapshot.result) > len(result)):
                snapshot.result = result
            snapshot.is_error = is_error
            snapshot.output_size = int(meta.get("output_size") or len(snapshot.result))
            snapshot.truncated = bool(meta.get("truncated"))
            snapshot.agent_visible_truncated = bool(meta.get("agent_visible_truncated"))
            snapshot.transport_truncated = bool(meta.get("transport_truncated"))
            snapshot.has_full_output = bool(meta.get("has_full_output"))
            snapshot.recovery_call_id = (
                meta.get("recovery_call_id")
                if isinstance(meta.get("recovery_call_id"), str)
                else None
            )
            snapshot.tool_output_artifact_id = (
                meta.get("tool_output_artifact_id")
                if isinstance(meta.get("tool_output_artifact_id"), str)
                else None
            )
            snapshot.anchors_available = bool(meta.get("anchors_available"))
            snapshot.anchor_count = int(meta.get("anchor_count") or 0)
            snapshot.updated_at = _utcnow()
            self._active_tool_outputs.pop(key, None)
        self._tool_output_spool.mark_complete(
            conversation_id=conversation_id,
            session_id=session_id,
            call_id=call_id,
            tool_name=tool_name,
            turn_id=turn_id,
            status="failed" if is_error else "completed",
        )
        await self._persist_active_tool_output_l2(conversation_id)

    def read_live_tool_output_page(
        self,
        *,
        conversation_id: str,
        session_id: str,
        call_id: str,
        offset: int = 0,
        limit: int = 200,
        latest: bool = False,
    ) -> ToolOutputSpoolPage | None:
        return self._tool_output_spool.page(
            conversation_id=conversation_id,
            session_id=session_id,
            call_id=call_id,
            offset=offset,
            limit=limit,
            latest=latest,
        )

    async def _append_active_stream_chunk(
        self,
        *,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        delta: str,
    ) -> tuple[int, int]:
        """Append a live token to the volatile stream snapshot.

        Returns the zero-based chunk index and starting content offset for the
        chunk. Transport clients use these values to make chunk application
        idempotent across reconnects and foreground reconciliation.
        """

        async with self._active_streams_lock:
            stream = self._active_streams.get(conversation_id)
            if (
                stream is None
                or stream.session_id != session_id
                or stream.message_id != message_id
                or stream.turn_id != turn_id
            ):
                stream = ActiveStreamState(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                )
                self._active_streams[conversation_id] = stream
            index = stream.chunk_count
            offset = _utf16_code_units(stream.content)
            stream.content += delta
            stream.chunk_count += 1
            stream.updated_at = _utcnow()
            return index, offset

    async def _reset_active_stream(self, conversation_id: str) -> None:
        async with self._active_streams_lock:
            self._active_streams.pop(conversation_id, None)

    async def _pop_active_stream(
        self,
        *,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
    ) -> ActiveStreamState | None:
        async with self._active_streams_lock:
            stream = self._active_streams.get(conversation_id)
            if (
                stream is None
                or stream.session_id != session_id
                or stream.message_id != message_id
                or stream.turn_id != turn_id
            ):
                return None
            self._active_streams.pop(conversation_id, None)
            return stream

    async def _persist_cancelled_active_stream(
        self,
        *,
        conversation_id: str,
        session: SessionModel,
        message_id: str,
        turn_id: str | None,
        user_email: str,
        agent: AgentDefinition,
    ) -> tuple[str | None, int]:
        """Persist already streamed assistant text when a turn is cancelled."""

        stream = await self._pop_active_stream(
            conversation_id=conversation_id,
            session_id=session.session_id,
            message_id=message_id,
            turn_id=turn_id,
        )
        if stream is None or not stream.content:
            return None, 0

        event = SessionEvent(
            type="assistant_message",
            data={
                "content": stream.content,
                "turn_id": turn_id,
                "partial": True,
                "cancelled": True,
                "finish_reason": "user_cancelled",
            },
        )
        intaris_session_id = session.intaris_session_id or session.session_id
        digest = hashlib.sha256(stream.content.encode("utf-8")).hexdigest()[:16]
        idempotency_key = (
            f"cancelled-active-stream:{intaris_session_id}:"
            f"{turn_id or message_id}:{stream.chunk_count}:{digest}"
        )
        try:
            append_result = await self._providers.guardrails.record_events(
                session_id=intaris_session_id,
                events=[event],
                source="cognis",
                idempotency_key=idempotency_key,
                user_email=user_email,
                agent_id=agent.agent_id,
                agent_owner_email=getattr(agent, "owner_email", user_email),
            )
            if not append_result.ok:
                raise RuntimeError("Intaris did not persist cancelled assistant stream")
            await self._session_cache.append_recorded_events(session, [event], append_result)
            return stream.content, append_result.last_seq
        except Exception:
            logger.warning(
                "turn_scheduler: failed to persist cancelled assistant stream",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "turn_id": turn_id,
                    }
                },
                exc_info=True,
            )
            return None, 0

    async def _persist_follow_up_turn_notice(
        self,
        *,
        conversation_id: str,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        follow_up: FollowUpMetadata,
        turn_id: str,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...],
    ) -> None:
        """Persist and broadcast the visible notice for a follow-up initiated turn."""

        text = render_follow_up_turn_notice(follow_up)
        notice_id = f"turn-init:{follow_up.follow_up_id}"
        data: dict[str, Any] = {
            "content": text,
            "text": text,
            "message": text,
            "event": "turn_initiated",
            "notice_id": notice_id,
            "kind": "turn_initiated",
            "scope": "turn",
            "turn_id": turn_id,
            "follow_up_id": follow_up.follow_up_id,
            "origin_kind": follow_up.origin_kind.value,
            "status": follow_up.status.value,
        }
        topic_ref = getattr(follow_up, "topic_ref", None)
        if topic_ref:
            data["source_id"] = topic_ref
        source_title = getattr(follow_up, "task_title", None) or getattr(follow_up, "title", None)
        if source_title:
            data["source_title"] = source_title

        try:
            event = SessionEvent(type="system_message", data=data)
            session_id = session.session_id
            intaris_session_id = getattr(session, "intaris_session_id", None) or session_id
            idempotency_key = f"{intaris_session_id}:turn_initiated:{follow_up.follow_up_id}"
            append_result = await self._providers.guardrails.record_events(
                session_id=intaris_session_id,
                events=[event],
                source="cognis",
                idempotency_key=idempotency_key,
                user_email=user_email,
                agent_id=agent.agent_id,
                agent_owner_email=getattr(agent, "owner_email", user_email),
            )
            if not append_result.ok:
                raise RuntimeError("Intaris did not persist follow-up turn notice")
            if append_result.count <= 0:
                return
            await self._session_cache.append_recorded_events(session, [event], append_result)
            await self._notify_observers_system_message(
                conversation_id,
                text,
                notice_id=notice_id,
                kind="turn_initiated",
                scope="turn",
                turn_id=turn_id,
                turn_observers=turn_observers,
            )
        except Exception:
            logger.warning(
                "turn_scheduler: failed to persist follow-up turn notice",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": getattr(session, "session_id", None),
                        "turn_id": turn_id,
                        "follow_up_id": follow_up.follow_up_id,
                    }
                },
                exc_info=True,
            )

    async def _clear_redo_on_accepted_user_turn(
        self,
        conversation_id: str,
        *,
        content: str,
        system_initiated: bool,
    ) -> None:
        if system_initiated or is_system_slash_command_message(content):
            return
        try:
            async with self._session_factory() as db_session:
                await queries.clear_conversation_history_rebase_metadata(
                    db_session, conversation_id
                )
                await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to clear redo metadata for accepted user turn",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )

    async def _mark_managed_conversation_turn_running(
        self,
        *,
        target_conversation_id: str,
        target_session_id: str,
        turn_id: str | None,
    ) -> None:
        """Mirror scheduler-owned launches into the managed-conversation link."""

        try:
            async with self._session_factory() as db_session:
                link = await queries.get_managed_conversation_link_for_target(
                    db_session,
                    target_conversation_id,
                )
                if link is None:
                    return
                await queries.update_managed_conversation_link(
                    db_session,
                    link.link_id,
                    conversation_state="open",
                    turn_state="running",
                    target_session_id=target_session_id,
                    active_turn_id=turn_id,
                    last_error=None,
                )
                await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to mark managed conversation turn running",
                extra={
                    "extra_data": {
                        "target_conversation_id": target_conversation_id,
                        "target_session_id": target_session_id,
                        "turn_id": turn_id,
                    }
                },
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_turn(
        self,
        conversation_id: str,
        content: str,
        *,
        user_email: str,
        attachments: list[dict[str, Any]] | None = None,
        outbound_attachments: list[dict[str, Any]] | None = None,
        system_initiated: bool = False,
        follow_up: FollowUpMetadata | None = None,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
        client_message_id: str | None = None,
        queued_message_id: str | None = None,
        prepared_attachment_notice: str | None = None,
        prepared_attachment_context: str | None = None,
        one_shot_chat_mode: ChatMode | None = None,
    ) -> TurnError | None:
        """Submit a chat turn for execution.

        Returns a ``TurnError`` immediately if the turn cannot be started
        (authorization failure, session blocked, escalation pending, etc.).
        Returns ``None`` on successful submission — results are delivered
        via ``TurnObserver`` callbacks and EventBus lifecycle events.

        Turns are serialized per conversation. If a turn is already active,
        the message is queued up to ``session.max_queued_messages``.
        """
        normalized_attachments, attachment_error = await self._resolve_attachments_for_turn(
            user_email=user_email,
            attachments=attachments or [],
        )
        if attachment_error is not None:
            return attachment_error

        # Used ONLY for conversation bootstrapping (title generation, intention bootstrap).
        # All other paths use the raw content so that attachment-only messages record and
        # broadcast an empty string, which the UI optimistic-bubble deduplication expects.
        bootstrap_content = _effective_user_content(content, normalized_attachments)

        # Load conversation runtime only after validating attachments so
        # failed first sends do not bootstrap a session unnecessarily.
        try:
            runtime = await self._load_conversation_runtime(
                conversation_id, user_message=bootstrap_content
            )
        except SessionCreationFailedError:
            return TurnError(
                code="session_creation_failed",
                message="Could not create a session. Try again or check the diagnostics page.",
                recoverable=True,
            )
        if runtime is None:
            return TurnError(
                code="not_found",
                message="Conversation not found",
                recoverable=False,
            )

        conversation, session, agent, bootstrap_wait_for_intention = runtime
        if one_shot_chat_mode is None and not system_initiated:
            directive = parse_chat_mode_directive(content)
            if directive is not None and directive.one_shot and directive.remaining_content:
                one_shot_chat_mode = directive.mode
                content = directive.remaining_content
                bootstrap_content = _effective_user_content(content, normalized_attachments)

        # Authorization check
        if not system_initiated and conversation.user_email != user_email:
            return TurnError(
                code="forbidden",
                message="Conversation access denied",
                recoverable=False,
            )

        if prepared_attachment_notice is None and prepared_attachment_context is None:
            attachment_notice, attachment_context = await self._build_attachment_support_messages(
                session=session,
                agent=agent,
                attachments=normalized_attachments,
            )
        else:
            attachment_notice = prepared_attachment_notice
            attachment_context = prepared_attachment_context

        # Conversation state check
        if conversation.status in {"archived", "deleted"}:
            return TurnError(
                code="conflict",
                message="Conversation is not active",
                recoverable=False,
            )

        # Session state check
        if session.status in BLOCKED_STATES:
            if session.status == SessionStatus.SUSPENDED:
                return TurnError(
                    code="session_suspended",
                    message="Session is suspended. Resolve the pending escalation to continue.",
                    recoverable=True,
                )
            return TurnError(
                code="session_ended",
                message="This session has ended. Use /new to start a fresh conversation.",
                recoverable=False,
            )

        # Escalation-pending check
        if not system_initiated:
            pending_esc = self._pause_waiter.find_pending(
                pause_type="escalation",
                conversation_id=conversation_id,
            )
            if pending_esc is not None:
                queue = self._queued_messages[conversation_id]
                if client_message_id and any(
                    queued.client_message_id == client_message_id for queued in queue
                ):
                    await self._notify_queue_updated(conversation_id, turn_observers=turn_observers)
                    return None
                # Queue the message behind the escalation
                await self._touch_conversation(conversation_id)
                queue.append(
                    _QueuedMessage(
                        queue_id=self._new_queue_id(),
                        content=content,
                        user_email=user_email,
                        client_message_id=client_message_id,
                        attachments=[
                            item.model_dump(mode="json") for item in normalized_attachments
                        ],
                        attachment_notice=attachment_notice,
                        attachment_context=attachment_context,
                        outbound_attachments=outbound_attachments,
                        follow_up=follow_up,
                        channel_deliverable=channel_deliverable,
                        delivery_id=delivery_id,
                        delivery_fallback_text=delivery_fallback_text,
                        turn_observers=tuple(turn_observers or ()),
                        one_shot_chat_mode=one_shot_chat_mode,
                    )
                )
                await self._clear_redo_on_accepted_user_turn(
                    conversation_id,
                    content=content,
                    system_initiated=system_initiated,
                )
                await self._notify_queue_updated(conversation_id, turn_observers=turn_observers)
                last_notified_pause_id = self._escalation_notice_pause_ids.get(conversation_id)
                if last_notified_pause_id != pending_esc.pause_id:
                    self._escalation_notice_pause_ids[conversation_id] = pending_esc.pause_id
                    await self._notify_observers_system_message(
                        conversation_id,
                        "Waiting for escalation resolution. "
                        "Use /approve or /deny, or use the buttons above.",
                    )
                return None
            self._escalation_notice_pause_ids.pop(conversation_id, None)

            pending_questions = self._pause_waiter.list_pending(
                conversation_id=conversation_id,
                pause_type="step_question",
            )
            if any(pause.task_id is None for pause in pending_questions):
                return TurnError(
                    code="pending_question",
                    message="Answer the pending question before sending a new message.",
                    recoverable=True,
                )
            for pause_type in ("auth_challenge", "credential_request"):
                pending_inputs = self._pause_waiter.list_pending(
                    conversation_id=conversation_id,
                    pause_type=pause_type,
                )
                if any(pause.task_id is None for pause in pending_inputs):
                    return TurnError(
                        code="pending_input_request",
                        message="Answer the pending input request before sending a new message.",
                        recoverable=True,
                    )

        max_active_turns, max_queued_messages = await self._load_turn_limits()

        async with self._turn_lock(conversation_id):
            # Per-user concurrent turn limit
            if not system_initiated:
                user_active = self._user_turn_counts.get(user_email, 0)
                if user_active >= max_active_turns:
                    return TurnError(
                        code="rate_limited",
                        message="Too many concurrent turns. Wait for a turn to finish.",
                        recoverable=True,
                    )

            # Queue if a turn is already active or still cancelling.  This lock is
            # the hard per-conversation serialization boundary: a new turn must
            # not be launched until the previous task has run its final cleanup.
            active = self._active_turns.get(conversation_id)
            if active is not None:
                if active.done():
                    self._active_turns.pop(conversation_id, None)
                else:
                    queue = self._queued_messages[conversation_id]
                    if client_message_id and any(
                        queued.client_message_id == client_message_id for queued in queue
                    ):
                        await self._notify_queue_updated(
                            conversation_id, turn_observers=turn_observers
                        )
                        return None
                    if len(queue) >= max_queued_messages:
                        return TurnError(
                            code="queue_full",
                            message="Message queue is full. Wait for the current turn to finish.",
                            recoverable=True,
                        )
                    if not system_initiated:
                        await self._touch_conversation(conversation_id)
                    queue.append(
                        _QueuedMessage(
                            queue_id=self._new_queue_id(),
                            content=content,
                            user_email=user_email,
                            client_message_id=client_message_id,
                            attachments=[
                                item.model_dump(mode="json") for item in normalized_attachments
                            ],
                            attachment_notice=attachment_notice,
                            attachment_context=attachment_context,
                            outbound_attachments=outbound_attachments,
                            system_initiated=system_initiated,
                            follow_up=follow_up,
                            channel_deliverable=channel_deliverable,
                            delivery_id=delivery_id,
                            delivery_fallback_text=delivery_fallback_text,
                            turn_observers=tuple(turn_observers or ()),
                            one_shot_chat_mode=one_shot_chat_mode,
                        )
                    )
                    await self._clear_redo_on_accepted_user_turn(
                        conversation_id,
                        content=content,
                        system_initiated=system_initiated,
                    )
                    await self._notify_queue_updated(conversation_id, turn_observers=turn_observers)
                    return None

            checkpoint_conversation = _model_copy_or_self(conversation)
            checkpoint_session = _model_copy_or_self(session)

            if session.status == SessionStatus.IDLE:
                try:
                    updated = await self._session_manager.mark_active(session.session_id)
                    if updated:
                        session.status = SessionStatus.ACTIVE
                        session.idle_since = None
                except Exception:
                    logger.warning(
                        "turn_scheduler: failed to reactivate idle session",
                        extra={
                            "extra_data": {
                                "conversation_id": conversation_id,
                                "session_id": session.session_id,
                            }
                        },
                        exc_info=True,
                    )

            if not system_initiated:
                checkpoint_result = await self._maybe_idle_checkpoint_compact(
                    conversation=checkpoint_conversation,
                    session=checkpoint_session,
                    agent=agent,
                )
                if checkpoint_result.session_id != checkpoint_session.session_id:
                    session = checkpoint_result
                    conversation.active_session_id = session.session_id

            await self._clear_redo_on_accepted_user_turn(
                conversation_id,
                content=content,
                system_initiated=system_initiated,
            )

            # Launch the turn while still holding the conversation lock so no
            # other submitter can observe a gap between admission and ownership
            # registration.
            if not system_initiated:
                await self._touch_conversation(conversation_id)
            self._launch_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                content=content,
                user_email=user_email,
                attachments=normalized_attachments,
                outbound_attachments=outbound_attachments,
                attachment_notice=attachment_notice,
                attachment_context=attachment_context,
                system_initiated=system_initiated,
                follow_up=follow_up,
                channel_deliverable=channel_deliverable,
                delivery_id=delivery_id,
                delivery_fallback_text=delivery_fallback_text,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                turn_observers=tuple(turn_observers or ()),
                client_message_id=client_message_id,
                queue_id=queued_message_id,
                one_shot_chat_mode=one_shot_chat_mode,
            )
        return None

    async def cancel_turn(self, conversation_id: str, *, clear_queue: bool = True) -> bool:
        """Cancel the active turn and all its child sub-sessions.

        ``clear_queue`` is reserved for explicit user stop commands. UI stop
        controls should cancel only the active turn so already queued messages
        remain pending and run after the cancelled turn settles.
        """
        control = self._turn_controls.get(conversation_id)
        queue = self._queued_messages.get(conversation_id)
        cleared_queue = False
        if clear_queue and queue is not None:
            cleared_queue = bool(queue)
            for queued in queue:
                if queued.follow_up is not None:
                    await self._clear_follow_up_pending(
                        conversation_id, queued.follow_up.follow_up_id
                    )
            queue.clear()
        if cleared_queue:
            await self._notify_queue_updated(conversation_id)
        if control is None:
            return cleared_queue
        if isinstance(control, asyncio.Event):
            control.set()
        else:
            control.cancel_event.set()
        active_task = self._active_turns.get(conversation_id)
        if active_task is not None and not active_task.done():
            active_task.cancel()
        # Also cancel child sub-sessions via the agent loop
        session_id = self._turn_sessions.get(conversation_id)
        if session_id:
            cancelled = await self._agent_loop.cancel_children(session_id)
            if cancelled:
                logger.info(
                    "turn_scheduler: cancelled child sub-sessions",
                    extra={"extra_data": {"count": cancelled, "session_id": session_id}},
                )
        return True

    def has_active_turn(self, conversation_id: str) -> bool:
        """Check if a turn is currently active for a conversation."""
        active = self._active_turns.get(conversation_id)
        return active is not None and not active.done()

    def active_turn_checkpoint(self, conversation_id: str) -> dict[str, str | None] | None:
        """Return active turn identity used to fork from the last completed checkpoint."""

        active = self._active_turns.get(conversation_id)
        if active is None or active.done():
            return None
        control = self._turn_controls.get(conversation_id)
        return {
            "session_id": self._turn_sessions.get(conversation_id),
            "turn_id": control.turn_id if control is not None else None,
        }

    def running_turn_state(self, conversation_id: str) -> dict[str, Any] | None:
        """Return visible running-turn state for a conversation.

        A turn may remain internally busy for cleanup and queue draining after
        the user-visible work has settled.
        """

        active = self._active_turns.get(conversation_id)
        if active is None or active.done():
            return None
        control = self._turn_controls.get(conversation_id)
        if control and control.settled:
            return None
        return {
            "chat_mode": control.chat_mode if control else None,
            "chat_mode_source": control.chat_mode_source if control else None,
        }

    def has_running_turn(self, conversation_id: str) -> bool:
        """Check if a turn is still visibly running for the user."""

        return self.running_turn_state(conversation_id) is not None

    async def wait_for_turn(
        self,
        conversation_id: str,
        *,
        timeout_seconds: int | None = None,
    ) -> TurnResult | TurnError | None:
        """Wait for a currently running turn, or return immediately when idle."""

        active = self._active_turns.get(conversation_id)
        if active is None or active.done():
            return None
        loop = asyncio.get_running_loop()
        future: asyncio.Future[TurnResult | TurnError] = loop.create_future()
        self._turn_waiters[conversation_id].append(future)
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=max(1, int(timeout_seconds)))
        except TimeoutError:
            return None
        finally:
            with contextlib.suppress(ValueError):
                self._turn_waiters[conversation_id].remove(future)

    def active_turn_id(self, conversation_id: str) -> str | None:
        """Return active turn ID for a conversation, if any."""

        control = self._turn_controls.get(conversation_id)
        active = self._active_turns.get(conversation_id)
        if active is None or active.done() or control is None:
            return None
        return control.turn_id

    def queued_count(self, conversation_id: str) -> int:
        """Return the number of queued messages for a conversation."""
        return len(self._queued_messages.get(conversation_id, []))

    def queued_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return safe metadata for pending queued messages."""
        return [
            queued.snapshot(position=index + 1)
            for index, queued in enumerate(self._queued_messages.get(conversation_id, []))
        ]

    async def _load_turn_limits(self) -> tuple[int, int]:
        """Load user/conversation turn limits from DB-backed settings."""
        if not callable(self._session_factory):
            return DEFAULT_MAX_ACTIVE_TURNS_PER_USER, DEFAULT_MAX_QUEUED_MESSAGES
        async with self._session_factory() as db_session:
            max_active_turns_raw = await get_setting_value(
                db_session,
                "session.max_active_turns_per_user",
                DEFAULT_MAX_ACTIVE_TURNS_PER_USER,
            )
            max_queued_messages_raw = await get_setting_value(
                db_session,
                "session.max_queued_messages",
                DEFAULT_MAX_QUEUED_MESSAGES,
            )
        return (
            _positive_int_setting(max_active_turns_raw, DEFAULT_MAX_ACTIVE_TURNS_PER_USER),
            _positive_int_setting(max_queued_messages_raw, DEFAULT_MAX_QUEUED_MESSAGES),
        )

    async def _load_idle_checkpoint_settings(self) -> tuple[int, int]:
        """Load idle checkpoint compaction settings for long-lived chats."""

        if not callable(self._session_factory):
            return (
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS,
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS,
            )
        async with self._session_factory() as db_session:
            threshold_raw = await get_setting_value(
                db_session,
                "session.long_lived_chat_idle_compaction_seconds",
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS,
            )
            min_events_raw = await get_setting_value(
                db_session,
                "session.long_lived_chat_idle_compaction_min_events",
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS,
            )
        return (
            _non_negative_int_setting(
                threshold_raw,
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_SECONDS,
            ),
            _positive_int_setting(
                min_events_raw,
                DEFAULT_LONG_LIVED_CHAT_IDLE_COMPACTION_MIN_EVENTS,
            ),
        )

    def _conversation_idle_seconds(
        self,
        conversation: ConversationModel,
        session: SessionModel,
        *,
        now: datetime,
    ) -> float | None:
        """Return idle age before the incoming turn touches the conversation."""

        candidates = (
            conversation.last_message_at,
            session.idle_since,
            session.updated_at,
            session.started_at,
            conversation.updated_at,
            conversation.created_at,
        )
        for candidate in candidates:
            normalized = _normalize_utc(candidate)
            if normalized is None:
                continue
            return max(0.0, (now - normalized).total_seconds())
        return None

    async def _maybe_idle_checkpoint_compact(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
    ) -> SessionModel:
        """Rotate long-lived chats that have been idle before handling the new turn."""

        if not is_long_lived_chat_context(getattr(conversation, "context", None)):
            return session
        threshold_seconds, min_events = await self._load_idle_checkpoint_settings()
        if threshold_seconds <= 0:
            return session
        idle_seconds = self._conversation_idle_seconds(
            conversation,
            session,
            now=datetime.now(UTC),
        )
        if idle_seconds is None or idle_seconds < threshold_seconds:
            return session
        lock = self._idle_checkpoint_locks.setdefault(conversation.conversation_id, asyncio.Lock())
        if lock.locked():
            return session
        async with lock:
            try:
                if self._agent_loop.session_is_locked(session.session_id):
                    return session
            except AttributeError:
                return session
            try:
                new_session = await self._agent_loop.run_idle_checkpoint_compaction(
                    conversation=conversation,
                    session=session,
                    agent=agent,
                    min_events=min_events,
                )
            except Exception:
                logger.warning(
                    "turn_scheduler: idle checkpoint compaction failed",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation.conversation_id,
                            "session_id": session.session_id,
                        }
                    },
                    exc_info=True,
                )
                return session
            if new_session is None:
                return session
            conversation.active_session_id = new_session.session_id
            if len(self._idle_checkpoint_locks) > _MAX_DEFERRED_LOCKS:
                stale_ids = [
                    cid
                    for cid, idle_lock in self._idle_checkpoint_locks.items()
                    if not idle_lock.locked()
                ]
                for cid in stale_ids[
                    : max(0, len(self._idle_checkpoint_locks) - _MAX_DEFERRED_LOCKS)
                ]:
                    self._idle_checkpoint_locks.pop(cid, None)
            return new_session

    async def _load_visible_conversation_title(
        self,
        conversation_id: str,
        fallback: str | None,
    ) -> str | None:
        """Return the latest persisted title, falling back to the in-memory title."""

        try:
            async with self._session_factory() as db_session:
                row = await queries.get_conversation(db_session, conversation_id)
                return row.title if row is not None else fallback
        except Exception:
            logger.debug(
                "turn_scheduler: failed to reload conversation title",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
            return fallback

    async def _adopt_late_intaris_title(
        self,
        conversation: ConversationModel,
        session: SessionModel,
    ) -> bool:
        """Adopt a title that Intaris produced after bootstrap reasoning."""

        intaris_session_id = getattr(session, "intaris_session_id", None) or session.session_id
        if not intaris_session_id:
            return False
        if not hasattr(conversation, "title_source"):
            return False
        if not can_adopt_intaris_title(conversation):
            return False

        try:
            intaris_session = await self._providers.guardrails.get_session(intaris_session_id)
            title = intaris_session.title
            if not title:
                return False
            async with self._session_factory() as db_session:
                changed = await sync_intaris_title(
                    db_session,
                    conversation,
                    title,
                    updated_at=intaris_session.updated_at,
                )
                if changed:
                    await db_session.commit()
                return changed
        except Exception:
            logger.debug(
                "turn_scheduler: failed to adopt late Intaris title",
                extra={
                    "extra_data": {
                        "conversation_id": conversation.conversation_id,
                        "session_id": session.session_id,
                        "intaris_session_id": intaris_session_id,
                    }
                },
                exc_info=True,
            )
            return False

    async def _handle_conversation_updated(self, event: Event) -> None:
        """Remember realtime title updates already published via EventBus."""

        conversation_id = event.data.get("conversation_id")
        title = event.data.get("title")
        if isinstance(conversation_id, str) and isinstance(title, str) and title.strip():
            self._published_title_updates[conversation_id] = title

    async def cancel_queued_message(self, conversation_id: str, queue_id: str) -> bool:
        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return False
        for index, queued in enumerate(queue):
            if queued.queue_id != queue_id:
                continue
            del queue[index]
            await self._notify_queue_updated(conversation_id)
            if queued.follow_up is not None:
                await self._clear_follow_up_pending(conversation_id, queued.follow_up.follow_up_id)
            return True
        return False

    async def update_queued_message(
        self, conversation_id: str, queue_id: str, *, content: str
    ) -> dict[str, Any] | None:
        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return None
        for index, queued in enumerate(queue):
            if queued.queue_id != queue_id:
                continue
            queued.content = content
            queued.updated_at = _utcnow()
            snapshot = queued.snapshot(position=index + 1)
            await self._notify_queue_updated(conversation_id)
            return snapshot
        return None

    @staticmethod
    def _new_queue_id() -> str:
        return f"qmsg_{uuid.uuid4().hex}"

    async def _notify_queue_updated(
        self,
        conversation_id: str,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        messages = self.queued_messages(conversation_id)
        observers = self._iter_observers(conversation_id, turn_observers=turn_observers)
        await asyncio.gather(
            *(
                self._call_observer(
                    conversation_id,
                    observer,
                    observer.on_queued,
                    conversation_id,
                    len(messages),
                )
                for observer in observers
            ),
            *(
                self._call_observer(
                    conversation_id,
                    observer,
                    observer.on_queued_messages,
                    conversation_id,
                    messages,
                )
                for observer in observers
                if callable(getattr(observer, "on_queued_messages", None))
            ),
        )

    async def _consume_queued_batch_for_active_turn(
        self,
        conversation_id: str,
        *,
        reason: str,
    ) -> list[dict[str, Any]]:
        """Drain the currently queued inbox batch for an active direct turn."""

        queue = self._queued_messages.get(conversation_id)
        if not queue:
            return []
        control = self._turn_controls.get(conversation_id)
        if control is None:
            return []

        batch: list[_QueuedMessage] = []
        while queue and self._queued_message_is_absorbable(queue[0]):
            batch.append(queue.popleft())
        if not batch:
            return []

        self._merge_active_turn_observers(control.turn_observers, batch)

        payloads: list[dict[str, Any]] = []
        for queued in batch:
            if queued.follow_up is not None:
                control.absorbed_follow_up_ids.add(queued.follow_up.follow_up_id)
            if queued.outbound_attachments:
                control.absorbed_outbound_attachments.extend(queued.outbound_attachments)
            if queued.channel_deliverable:
                control.absorbed_channel_deliverable = True
            if queued.delivery_id:
                control.absorbed_delivery_id = queued.delivery_id
            if queued.delivery_fallback_text:
                control.absorbed_delivery_fallback_text = queued.delivery_fallback_text
            payloads.append(
                {
                    "queue_id": queued.queue_id,
                    "client_message_id": queued.client_message_id,
                    "content": queued.content,
                    "attachments": list(queued.attachments or []),
                    "attachment_notice": queued.attachment_notice,
                    "attachment_context": queued.attachment_context,
                    "system_initiated": queued.system_initiated,
                    "follow_up": queued.follow_up,
                }
            )
            if queued.attachment_notice:
                await self._notify_observers_system_message(
                    conversation_id,
                    queued.attachment_notice,
                    turn_observers=control.turn_observers,
                )
            if not queued.system_initiated:
                await self._event_bus.publish(
                    Event(
                        type=EventType.USER_MESSAGE,
                        data=_user_message_event_payload(
                            conversation_id=conversation_id,
                            session_id=self._turn_sessions.get(conversation_id),
                            content=queued.content,
                            attachments=strip_attachment_payload_bytes(queued.attachments or []),
                            queue_id=queued.queue_id,
                            client_message_id=queued.client_message_id,
                        ),
                    )
                )

        logger.info(
            "turn_scheduler: absorbed queued batch into active turn",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "batch_size": len(payloads),
                    "remaining_queue": self.queued_count(conversation_id),
                }
            },
        )
        await self._notify_queue_updated(conversation_id, turn_observers=control.turn_observers)
        return payloads

    def _queued_message_is_absorbable(self, queued: _QueuedMessage) -> bool:
        """Return whether a queued message can be merged mid-turn."""

        return all(
            getattr(observer, "supports_mid_turn_absorb", False)
            for observer in queued.turn_observers
        )

    def _merge_active_turn_observers(
        self,
        active_observers: list[TurnObserver],
        batch: list[_QueuedMessage],
    ) -> None:
        """Merge queued per-submit observers into the active turn."""

        for queued in batch:
            for observer in queued.turn_observers:
                absorbed = False
                for active_observer in active_observers:
                    absorb = getattr(active_observer, "absorb_queued_observer", None)
                    if callable(absorb) and absorb(observer):
                        absorbed = True
                        break
                if absorbed:
                    continue
                if not getattr(observer, "supports_mid_turn_absorb", False):
                    continue
                if observer not in active_observers:
                    active_observers.append(observer)

    async def _purge_expired_follow_ups(self) -> None:
        now = monotonic()
        expired = [
            key
            for key, handled_at in self._handled_follow_ups.items()
            if now - handled_at >= FOLLOW_UP_DEDUPE_TTL_SECONDS
        ]
        for key in expired:
            self._handled_follow_ups.pop(key, None)
        try:
            async with self._session_factory() as db_session:
                await db_session.execute(
                    delete(FollowUpDedupeRow)
                    .where(FollowUpDedupeRow.expires_at <= _utcnow())
                    .execution_options(synchronize_session=False)
                )
                await db_session.commit()
        except Exception:
            logger.debug("turn_scheduler: durable follow-up purge unavailable", exc_info=True)

    @staticmethod
    def _follow_up_dedupe_key(conversation_id: str, follow_up_id: str) -> str:
        return f"{conversation_id}:{follow_up_id}"

    async def _register_follow_up(self, conversation_id: str, follow_up_id: str) -> bool:
        await self._purge_expired_follow_ups()
        key = (conversation_id, follow_up_id)
        now = _utcnow()
        expires_at = now + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS)
        dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
        try:
            async with self._session_factory() as db_session:
                db_session.add(
                    FollowUpDedupeRow(
                        dedupe_key=dedupe_key,
                        conversation_id=conversation_id,
                        follow_up_id=follow_up_id,
                        status="pending",
                        expires_at=expires_at,
                    )
                )
                await db_session.commit()
            self._pending_follow_ups.add(key)
            return True
        except IntegrityError:
            async with self._session_factory() as db_session:
                await db_session.rollback()
                row = await db_session.get(FollowUpDedupeRow, dedupe_key)
                if row is not None and _is_expired_timestamp(row.expires_at, now=now):
                    refreshed = await db_session.execute(
                        update(FollowUpDedupeRow)
                        .where(
                            FollowUpDedupeRow.dedupe_key == dedupe_key,
                            FollowUpDedupeRow.expires_at <= now,
                        )
                        .values(status="pending", expires_at=expires_at, updated_at=now)
                        .execution_options(synchronize_session=False)
                    )
                    await db_session.commit()
                    if refreshed.rowcount:
                        self._pending_follow_ups.add(key)
                        return True
                reason = row.status if row is not None else "handled"
                FOLLOW_UP_DEDUPE_TOTAL.labels(reason=reason).inc()
                return False
        except Exception:
            logger.debug("turn_scheduler: durable follow-up register unavailable", exc_info=True)
            if key in self._pending_follow_ups:
                FOLLOW_UP_DEDUPE_TOTAL.labels(reason="pending").inc()
                return False
            if key in self._handled_follow_ups:
                FOLLOW_UP_DEDUPE_TOTAL.labels(reason="handled").inc()
                return False
            self._pending_follow_ups.add(key)
            return True

    async def _mark_follow_up_handled(self, conversation_id: str, follow_up_id: str) -> None:
        key = (conversation_id, follow_up_id)
        self._pending_follow_ups.discard(key)
        self._handled_follow_ups[key] = monotonic()
        dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
        try:
            async with self._session_factory() as db_session:
                row = await db_session.get(FollowUpDedupeRow, dedupe_key)
                if row is None:
                    db_session.add(
                        FollowUpDedupeRow(
                            dedupe_key=dedupe_key,
                            conversation_id=conversation_id,
                            follow_up_id=follow_up_id,
                            status="handled",
                            expires_at=_utcnow() + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS),
                        )
                    )
                else:
                    row.status = "handled"
                    row.expires_at = _utcnow() + timedelta(seconds=FOLLOW_UP_DEDUPE_TTL_SECONDS)
                    row.updated_at = _utcnow()
                await db_session.commit()
        except Exception:
            logger.debug(
                "turn_scheduler: durable follow-up handled mark unavailable", exc_info=True
            )

    async def _clear_follow_up_pending(self, conversation_id: str, follow_up_id: str) -> None:
        self._pending_follow_ups.discard((conversation_id, follow_up_id))
        dedupe_key = self._follow_up_dedupe_key(conversation_id, follow_up_id)
        try:
            async with self._session_factory() as db_session:
                await db_session.execute(
                    delete(FollowUpDedupeRow)
                    .where(
                        FollowUpDedupeRow.dedupe_key == dedupe_key,
                        FollowUpDedupeRow.status == "pending",
                    )
                    .execution_options(synchronize_session=False)
                )
                await db_session.commit()
        except Exception:
            logger.debug("turn_scheduler: durable follow-up clear unavailable", exc_info=True)

    def _build_tool_call_ceiling_follow_up(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        metadata: dict[str, Any],
        prior_follow_up: FollowUpMetadata | None,
    ) -> ContinuationFollowUp | None:
        if (
            isinstance(prior_follow_up, ContinuationFollowUp)
            and prior_follow_up.reason == "tool_call_ceiling_reached"
        ):
            attempt = prior_follow_up.attempt + 1
        else:
            attempt = 1
        if attempt > MAX_AUTOMATIC_CONTINUATION_ATTEMPTS:
            return None

        pending_todos = _pending_todos_from_metadata(metadata)
        tool_call_count = _positive_optional_int(metadata.get("tool_call_count"))
        max_tool_calls = _positive_optional_int(metadata.get("max_tool_calls"))
        follow_up = ContinuationFollowUp(
            follow_up_id=build_follow_up_id(
                kind=FollowUpOriginKind.CONTINUATION.value,
                conversation_id=conversation_id,
                parts={
                    "reason": "tool_call_ceiling_reached",
                    "turn_id": turn_id,
                    "attempt": attempt,
                },
            ),
            mode=FollowUpMode.INTEGRATE,
            origin_kind=FollowUpOriginKind.CONTINUATION,
            relevance_hint=FollowUpRelevanceHint.SAME_THREAD,
            required_action=FollowUpRequiredAction.INTEGRATE_RESULT,
            topic_ref=turn_id,
            status=FollowUpStatus.COMPLETED,
            reason="tool_call_ceiling_reached",
            attempt=attempt,
            max_attempts=MAX_AUTOMATIC_CONTINUATION_ATTEMPTS,
            tool_call_count=tool_call_count,
            max_tool_calls=max_tool_calls,
            pending_todos=pending_todos,
        )
        return follow_up

    async def _schedule_tool_call_ceiling_continuation(
        self,
        *,
        conversation_id: str,
        session_id: str,
        turn_id: str,
        user_email: str,
        metadata: dict[str, Any],
        prior_follow_up: FollowUpMetadata | None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...],
    ) -> None:
        follow_up = self._build_tool_call_ceiling_follow_up(
            conversation_id=conversation_id,
            turn_id=turn_id,
            metadata=metadata,
            prior_follow_up=prior_follow_up,
        )
        tool_call_count = _positive_optional_int(metadata.get("tool_call_count"))
        max_tool_calls = _positive_optional_int(metadata.get("max_tool_calls"))
        if follow_up is None:
            message = (
                "Automatic continuation stopped after repeated tool-call ceilings. "
                "Send a new message to continue manually."
            )
            await self._notify_observers_system_message(
                conversation_id,
                message,
                turn_observers=turn_observers,
            )
            logger.warning(
                "turn_scheduler: automatic continuation ceiling exhausted",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "tool_call_count": tool_call_count,
                        "max_tool_calls": max_tool_calls,
                    }
                },
            )
            return

        count_text = (
            f" ({tool_call_count}/{max_tool_calls} tool calls)"
            if tool_call_count is not None and max_tool_calls is not None
            else ""
        )
        await self._notify_observers_system_message(
            conversation_id,
            f"Tool-call limit reached{count_text}. Continuing automatically.",
            turn_observers=turn_observers,
        )
        self._queued_messages[conversation_id].append(
            _QueuedMessage(
                content="",
                user_email=user_email,
                system_initiated=True,
                follow_up=follow_up,
                turn_observers=tuple(turn_observers),
            )
        )
        logger.info(
            "turn_scheduler: queued automatic continuation after tool-call ceiling",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "attempt": follow_up.attempt,
                    "tool_call_count": tool_call_count,
                    "max_tool_calls": max_tool_calls,
                    "pending_todo_count": len(follow_up.pending_todos),
                }
            },
        )
        await self._notify_queue_updated(conversation_id, turn_observers=turn_observers)

    # ------------------------------------------------------------------
    # Follow-up turn handling (EventBus subscriber)
    # ------------------------------------------------------------------

    async def _handle_follow_up_event(self, event: Event) -> None:
        """Handle a FOLLOW_UP_TURN_REQUESTED event."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            logger.warning("turn_scheduler: follow-up event missing conversation_id, dropping")
            return

        raw_follow_up = event.data.get("follow_up")
        if not isinstance(raw_follow_up, dict):
            logger.warning(
                "turn_scheduler: follow-up event missing typed metadata, dropping",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            return
        try:
            follow_up = parse_follow_up_metadata(raw_follow_up)
        except Exception as exc:
            logger.warning(
                "turn_scheduler: invalid follow-up metadata, dropping",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "follow_up_id": raw_follow_up.get("follow_up_id"),
                        "origin_kind": raw_follow_up.get("origin_kind"),
                        "error": str(exc),
                    }
                },
                exc_info=True,
            )
            return
        if not await self._register_follow_up(conversation_id, follow_up.follow_up_id):
            return

        # Use submit_turn for unified serialization
        # Determine user_email from the conversation
        async with self._session_factory() as db_session:
            row = await queries.get_conversation(db_session, conversation_id)
        if row is None:
            await self._clear_follow_up_pending(conversation_id, follow_up.follow_up_id)
            logger.warning(
                "turn_scheduler: follow-up conversation not found",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            return

        error = await self.submit_turn(
            conversation_id,
            "",
            user_email=row.user_email,
            attachments=event.data.get("attachments")
            if isinstance(event.data.get("attachments"), list)
            else None,
            outbound_attachments=event.data.get("attachments")
            if isinstance(event.data.get("attachments"), list)
            else None,
            system_initiated=True,
            follow_up=follow_up,
            channel_deliverable=bool(event.data.get("channel_deliverable")),
            delivery_id=event.data.get("delivery_id")
            if isinstance(event.data.get("delivery_id"), str)
            else None,
            delivery_fallback_text=event.data.get("delivery_fallback_text")
            if isinstance(event.data.get("delivery_fallback_text"), str)
            else None,
        )
        if error is not None:
            await self._publish_turn_error(
                conversation_id,
                row.active_session_id or "",
                error,
                system_initiated=True,
                channel_deliverable=bool(event.data.get("channel_deliverable")),
                delivery_id=event.data.get("delivery_id")
                if isinstance(event.data.get("delivery_id"), str)
                else None,
                delivery_fallback_text=event.data.get("delivery_fallback_text")
                if isinstance(event.data.get("delivery_fallback_text"), str)
                else None,
            )
            await self._clear_follow_up_pending(conversation_id, follow_up.follow_up_id)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def _resolve_attachments_for_turn(
        self,
        *,
        user_email: str,
        attachments: list[dict[str, Any]],
    ) -> tuple[list[AttachmentRef], TurnError | None]:
        if not attachments:
            return [], None
        from cognis.store.queries import get_artifact_record

        normalized: list[AttachmentRef] = []
        async with self._session_factory() as session:
            for raw in attachments:
                artifact_id = raw.get("artifact_id") if isinstance(raw, dict) else None
                if not isinstance(artifact_id, str) or not artifact_id:
                    return [], TurnError(
                        code="validation_error",
                        message="Invalid attachment reference",
                        recoverable=True,
                    )
                row = await get_artifact_record(session, artifact_id)
                if row is None or row.status == "deleted":
                    return [], TurnError(
                        code="not_found",
                        message="Attachment not found",
                        recoverable=True,
                    )
                if row.owner_email and row.owner_email != user_email:
                    return [], TurnError(
                        code="forbidden",
                        message="Attachment access denied",
                        recoverable=False,
                    )
                url = await self._artifact_store.async_get_public_url(
                    row.namespace,
                    row.object_id,
                    row.filename,
                )
                normalized.append(
                    AttachmentRef(
                        artifact_id=row.artifact_id,
                        kind=ArtifactKind(row.kind),
                        mime_type=row.mime_type,
                        filename=row.filename,
                        size_bytes=row.size_bytes,
                        url=url,
                    )
                )
        return normalized, None

    async def _build_attachment_support_messages(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
    ) -> tuple[str | None, str | None]:
        if not attachments:
            return None, None
        explicit_model = self._session_cache.get_model_override(session.session_id) or (
            agent.llm_config.model if agent.llm_config else None
        )
        explicit_provider_id = agent.llm_config.provider_id if agent.llm_config else None
        provider_id: str | None = None
        if hasattr(self._providers.llm, "resolve_model_target"):
            try:
                resolved_model, provider_id = await self._providers.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                )
            except TypeError:
                resolved_model, provider_id = await self._providers.llm.resolve_model_target(
                    explicit_model=explicit_model,
                    task_type="default",
                )
        else:
            try:
                resolved_model = await self._providers.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                    explicit_provider_id=explicit_provider_id,
                )
            except TypeError:
                resolved_model = await self._providers.llm.resolve_model(
                    explicit_model=explicit_model,
                    task_type="default",
                )
        if provider_id is not None:
            try:
                model_info = await self._providers.llm.get_model_info(
                    resolved_model,
                    provider_id=provider_id,
                )
            except TypeError:
                model_info = await self._providers.llm.get_model_info(resolved_model)
        else:
            model_info = await self._providers.llm.get_model_info(resolved_model)
        unsupported: list[str] = []
        pdf_fallbacks: list[str] = []
        for attachment in attachments:
            if attachment.kind == ArtifactKind.IMAGE and model_info.supports_vision:
                continue
            if attachment.kind == ArtifactKind.PDF and (
                model_info.supports_pdf_input or model_info.supports_file_input
            ):
                continue
            if attachment.kind == ArtifactKind.AUDIO and (
                model_info.supports_audio_input or model_info.supports_file_input
            ):
                continue
            if (
                attachment.kind in {ArtifactKind.FILE, ArtifactKind.VIDEO}
                and model_info.supports_file_input
            ):
                continue
            if attachment.kind == ArtifactKind.PDF:
                extracted = await self._extract_pdf_text(attachment)
                if extracted:
                    pdf_fallbacks.append(extracted)
                    unsupported.append(f"{attachment.filename} (using extracted text fallback)")
                    continue
            unsupported.append(f"{attachment.filename} ({attachment.kind.value})")

        notice = None
        if unsupported:
            joined = ", ".join(unsupported)
            notice = (
                f"The current model ({resolved_model}) cannot read some attachments natively: {joined}. "
                "Use artifact_read with the attachment artifact_id to inspect those files. "
                "artifact_read keeps the current model when it already supports the file and falls back to "
                "the attachment_analysis route only when needed. If extracted fallback text is available, use it "
                "carefully and mention any uncertainty."
            )
        if not pdf_fallbacks:
            return notice, None
        context = (
            '<attachment_context trust="untrusted">\n'
            "PDF files were converted to best-effort extracted text because the model lacks native PDF support. "
            "Formatting, tables, and OCR may be imperfect.\n\n"
            + "\n\n".join(pdf_fallbacks)
            + "\n</attachment_context>"
        )
        return notice, context

    async def _build_attachment_notice(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
    ) -> str | None:
        notice, _ = await self._build_attachment_support_messages(
            session=session,
            agent=agent,
            attachments=attachments,
        )
        return notice

    async def _build_attachment_context(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
    ) -> str | None:
        _notice, context = await self._build_attachment_support_messages(
            session=session,
            agent=agent,
            attachments=attachments,
        )
        return context

    async def _extract_pdf_text(self, attachment: AttachmentRef) -> str | None:
        from cognis.store.queries import get_artifact_record

        async with self._session_factory() as session:
            row = await get_artifact_record(session, attachment.artifact_id)
        if row is None:
            return None
        try:
            content, _content_type = await self._artifact_store.async_load(
                row.namespace,
                row.object_id,
                row.filename,
            )
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))
            chunks: list[str] = []
            for page in reader.pages[:8]:
                text = (page.extract_text() or "").strip()
                if text:
                    chunks.append(text)
                if sum(len(chunk) for chunk in chunks) >= 4000:
                    break
            if not chunks:
                return None
            combined = "\n\n".join(chunks)
            safe_filename = html.escape(attachment.filename)
            safe_text = html.escape(combined[:4000])
            return f"Extracted text from {safe_filename}:\n{safe_text}"
        except Exception:
            return None

    def _launch_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        content: str,
        user_email: str,
        attachments: list[AttachmentRef] | None = None,
        outbound_attachments: list[dict[str, Any]] | None = None,
        attachment_notice: str | None = None,
        attachment_context: str | None = None,
        system_initiated: bool = False,
        follow_up: FollowUpMetadata | None = None,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        bootstrap_wait_for_intention: bool = False,
        turn_observers: tuple[TurnObserver, ...] = (),
        client_message_id: str | None = None,
        queue_id: str | None = None,
        one_shot_chat_mode: ChatMode | None = None,
    ) -> None:
        """Launch a turn as a background asyncio.Task."""
        conversation_id = conversation.conversation_id
        control = _TurnControl(turn_observers=list(turn_observers))
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        control.turn_id = turn_id
        self._turn_controls[conversation_id] = control
        self._turn_sessions[conversation_id] = session.session_id
        if not system_initiated:
            self._user_turn_counts[user_email] = self._user_turn_counts.get(user_email, 0) + 1
        task_holder: dict[str, asyncio.Task[None]] = {}

        async def _runner() -> None:
            owner_task = task_holder["task"]
            await self._run_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                content=content,
                user_email=user_email,
                attachments=attachments,
                outbound_attachments=outbound_attachments,
                attachment_notice=attachment_notice,
                attachment_context=attachment_context,
                system_initiated=system_initiated,
                follow_up=follow_up,
                channel_deliverable=channel_deliverable,
                delivery_id=delivery_id,
                delivery_fallback_text=delivery_fallback_text,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                cancel_event=control.cancel_event,
                turn_control=control,
                turn_id=turn_id,
                client_message_id=client_message_id,
                queue_id=queue_id,
                one_shot_chat_mode=one_shot_chat_mode,
                owner_task=owner_task,
            )

        task = asyncio.create_task(_runner())
        task_holder["task"] = task
        self._active_turns[conversation_id] = task

    async def _run_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        content: str,
        user_email: str,
        attachments: list[AttachmentRef] | None,
        outbound_attachments: list[dict[str, Any]] | None,
        attachment_notice: str | None,
        attachment_context: str | None,
        system_initiated: bool,
        follow_up: FollowUpMetadata | None = None,
        channel_deliverable: bool,
        delivery_id: str | None,
        delivery_fallback_text: str | None,
        bootstrap_wait_for_intention: bool,
        cancel_event: asyncio.Event,
        turn_control: _TurnControl | None = None,
        turn_id: str | None = None,
        turn_observers: tuple[TurnObserver, ...] = (),
        client_message_id: str | None = None,
        queue_id: str | None = None,
        one_shot_chat_mode: ChatMode | None = None,
        owner_task: asyncio.Task[None] | None = None,
    ) -> None:
        """Execute a single chat turn."""
        conversation_id = conversation.conversation_id
        turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
        message_id = turn_id
        _pre_turn_title = conversation.title
        start_time = asyncio.get_running_loop().time()
        turn_type = "system" if system_initiated else "user"
        turn_succeeded = False
        if turn_control is None:
            turn_control = _TurnControl(turn_observers=list(turn_observers))
        turn_control.turn_id = turn_id
        turn_observers = turn_control.turn_observers

        await self._mark_managed_conversation_turn_running(
            target_conversation_id=conversation_id,
            target_session_id=session.session_id,
            turn_id=turn_id,
        )

        resolved_chat_mode = ResolvedChatMode(mode="default", source="system_default")
        try:
            current_user_email.set(user_email)
            current_agent_id.set(agent.agent_id)
            current_agent_owner_email.set(getattr(agent, "owner_email", user_email))
            conversation_context = getattr(conversation, "context", None)
            platform_data = getattr(conversation_context, "platform_data", None) or {}
            resolved_chat_mode = resolve_chat_mode(
                conversation=conversation,
                agent=agent,
                one_shot_mode=one_shot_chat_mode,
            )
            turn_control.chat_mode = resolved_chat_mode.mode
            turn_control.chat_mode_source = resolved_chat_mode.source
            current_workspace_root.set(platform_data.get("workspace_root"))
            current_effective_working_directory.set(platform_data.get("working_directory"))
            refresh_policy = getattr(self._session_manager, "refresh_intaris_session_policy", None)
            if refresh_policy is not None:
                await refresh_policy(session)

            if attachment_notice:
                await self._notify_observers_system_message(
                    conversation_id,
                    attachment_notice,
                    turn_observers=turn_observers,
                )

            logger.info(
                "turn_scheduler: turn started",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "agent_id": agent.agent_id,
                        "system_initiated": system_initiated,
                    }
                },
            )

            # Publish TURN_STARTED lifecycle event
            await self._event_bus.publish(
                Event(
                    type=EventType.TURN_STARTED,
                    data={
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "message_id": message_id,
                        "turn_id": turn_id,
                        "chat_mode": resolved_chat_mode.mode,
                        "chat_mode_source": resolved_chat_mode.source,
                        "system_initiated": system_initiated,
                    },
                )
            )

            if system_initiated and follow_up is not None:
                await self._persist_follow_up_turn_notice(
                    conversation_id=conversation_id,
                    session=session,
                    agent=agent,
                    user_email=user_email,
                    follow_up=follow_up,
                    turn_id=turn_id,
                    turn_observers=turn_observers,
                )

            # Publish USER_MESSAGE so WebSocket clients watching this
            # conversation see channel-originated messages in real time
            # (without waiting for a page refresh / history reload).
            if not system_initiated:
                await self._event_bus.publish(
                    Event(
                        type=EventType.USER_MESSAGE,
                        data=_user_message_event_payload(
                            conversation_id=conversation_id,
                            session_id=session.session_id,
                            content=content,
                            turn_id=turn_id,
                            chat_mode=resolved_chat_mode.mode,
                            chat_mode_source=resolved_chat_mode.source,
                            attachments=[
                                item.model_dump(mode="json") for item in (attachments or [])
                            ],
                            client_message_id=client_message_id,
                            queue_id=queue_id,
                        ),
                    )
                )

            # Decision engine (skip for system-initiated turns)
            if not system_initiated:
                decision = await self._decision_engine.decide(
                    user_message=content,
                    agent=agent,
                )
            else:
                decision = None

            if decision is not None and getattr(decision, "decision", None) == "delegate":
                workflow_id = await self._select_workflow(
                    agent,
                    content,
                    project_id=getattr(conversation, "project_id", None),
                )
                task = await self._task_queue.submit(
                    created_by=user_email,
                    agent_id=agent.agent_id,
                    title=content[:80],
                    description=content,
                    created_by_agent_id=agent.agent_id,
                    source_type="chat",
                    source_ref=conversation_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    workflow_id=workflow_id,
                    project_id=getattr(conversation, "project_id", None),
                    workspace_root=current_workspace_root.get(),
                    working_directory=current_effective_working_directory.get(),
                    status="queued",
                )
                await self._notify_observers(
                    conversation_id,
                    "on_system_message",
                    conversation_id,
                    "Working on that in the background.",
                    turn_observers=turn_observers,
                )
                result = TurnResult(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    delegated=True,
                    task_id=task.task_id,
                    system_initiated=system_initiated,
                    channel_deliverable=channel_deliverable,
                    delivery_id=delivery_id,
                    delivery_fallback_text=delivery_fallback_text,
                    attachments=normalize_attachment_refs(outbound_attachments or []) or None,
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                )
                turn_control.settled = True
                await self._publish_turn_completed(result, turn_observers=turn_observers)
                TURNS_TOTAL.labels(outcome="delegated").inc()
                turn_succeeded = True
                return

            # Build streaming callbacks from observers
            (
                on_token,
                on_thinking,
                on_tool_call,
                on_tool_result,
                on_tool_progress,
                on_tool_output_chunk,
            ) = self._build_callbacks(
                conversation_id,
                session.session_id,
                message_id,
                turn_id,
                turn_observers=turn_observers,
            )

            # Execute the turn
            step_output = await self._workflow_engine.run_direct_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                user_message=content,
                user_attachments=attachments,
                attachment_notice=attachment_notice,
                attachment_context=attachment_context,
                system_initiated=system_initiated,
                follow_up=follow_up,
                on_progress=on_token,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                on_tool_progress=on_tool_progress,
                on_tool_output_chunk=on_tool_output_chunk,
                cancel_event=cancel_event,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                turn_id=turn_id,
                chat_mode=resolved_chat_mode,
                consume_boundary_batch=lambda reason: self._consume_queued_batch_for_active_turn(
                    conversation_id,
                    reason=reason,
                ),
            )
            step_error = _turn_error_from_step_output(step_output)
            if step_error is not None:
                logger.warning(
                    "turn_scheduler: turn step returned error",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "error_code": step_error.code,
                        }
                    },
                )
                turn_control.settled = True
                await self._publish_turn_error(
                    conversation_id,
                    session.session_id,
                    step_error,
                    turn_id=turn_id,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    turn_observers=turn_observers,
                )
                TURNS_TOTAL.labels(outcome="error").inc()
                return
            ceiling_metadata = _tool_call_ceiling_metadata(step_output)
            if (
                ceiling_metadata is not None
                and not channel_deliverable
                and getattr(conversation, "status", "active") == "active"
            ):
                await self._schedule_tool_call_ceiling_continuation(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    turn_id=turn_id,
                    user_email=user_email,
                    metadata=ceiling_metadata,
                    prior_follow_up=follow_up,
                    turn_observers=turn_observers,
                )

            queued_continuation_pending = self.queued_count(conversation_id) > 0
            # Post-turn housekeeping
            completed_at = datetime.now(UTC)
            await self._touch_conversation(conversation_id, when=completed_at)

            last_seq = 0
            try:
                entry = await self._session_cache.refresh(session)
                last_seq = entry.last_event_seq
            except Exception:
                logger.warning(
                    "turn_scheduler: post-turn session cache refresh failed",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                        }
                    },
                )
                cached = self._session_cache.get_entry(session.session_id)
                if cached is not None:
                    last_seq = cached.last_event_seq

            context_usage = self._session_cache.get_context_usage(session.session_id)

            await self._adopt_late_intaris_title(conversation, session)
            latest_title = await self._load_visible_conversation_title(
                conversation_id,
                conversation.title,
            )
            if latest_title is not None:
                conversation.title = latest_title

            # Check title change against the persisted row because Intaris title
            # sync may commit through a separate DB session inside the agent loop.
            already_published_title = self._published_title_updates.get(conversation_id)
            title_changed = bool(
                latest_title
                and latest_title != _pre_turn_title
                and latest_title != already_published_title
            )

            result = TurnResult(
                conversation_id=conversation_id,
                session_id=session.session_id,
                message_id=message_id,
                turn_id=turn_id,
                last_seq=last_seq,
                context_usage=context_usage,
                title_changed=title_changed,
                new_title=latest_title if title_changed else None,
                final_content=(
                    step_output.content.strip()
                    if step_output and step_output.content.strip()
                    else None
                ),
                system_initiated=system_initiated,
                channel_deliverable=(
                    channel_deliverable or turn_control.absorbed_channel_deliverable
                ),
                delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                delivery_fallback_text=(
                    turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                ),
                attachments=(
                    normalize_attachment_refs(
                        [
                            *(step_output.attachments if step_output else []),
                            *(outbound_attachments or []),
                            *turn_control.absorbed_outbound_attachments,
                        ]
                    )
                    or None
                ),
                completed_at=completed_at,
                chat_mode=resolved_chat_mode.mode,
                chat_mode_source=resolved_chat_mode.source,
                managed_continuation_pending=queued_continuation_pending,
            )
            turn_control.settled = True
            await self._publish_turn_completed(result, turn_observers=turn_observers)
            TURNS_TOTAL.labels(outcome="completed").inc()
            turn_succeeded = True

            logger.info(
                "turn_scheduler: turn completed",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "last_seq": last_seq,
                    }
                },
            )

        except asyncio.CancelledError:
            turn_control.settled = True
            completed_at = datetime.now(UTC)
            partial_content, last_seq = await self._persist_cancelled_active_stream(
                conversation_id=conversation_id,
                session=session,
                message_id=message_id,
                turn_id=turn_id,
                user_email=user_email,
                agent=agent,
            )
            if partial_content is not None:
                await self._touch_conversation(conversation_id, when=completed_at)
                result = TurnResult(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    turn_id=turn_id,
                    last_seq=last_seq,
                    final_content=partial_content,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    attachments=(
                        normalize_attachment_refs(
                            [
                                *(outbound_attachments or []),
                                *turn_control.absorbed_outbound_attachments,
                            ]
                        )
                        or None
                    ),
                    completed_at=completed_at,
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    partial=True,
                    finish_reason="user_cancelled",
                )
                await self._publish_turn_completed(result, turn_observers=turn_observers)
            error = TurnError(
                code="turn_cancelled",
                message="The current turn was cancelled.",
                recoverable=True,
            )
            if partial_content is None:
                await self._publish_turn_error(
                    conversation_id,
                    session.session_id,
                    error,
                    turn_id=turn_id,
                    system_initiated=system_initiated,
                    channel_deliverable=(
                        channel_deliverable or turn_control.absorbed_channel_deliverable
                    ),
                    delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                    delivery_fallback_text=(
                        turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                    ),
                    chat_mode=resolved_chat_mode.mode,
                    chat_mode_source=resolved_chat_mode.source,
                    turn_observers=turn_observers,
                )
            TURNS_TOTAL.labels(outcome="cancelled").inc()
            logger.info(
                "turn_scheduler: turn cancelled",
                extra={
                    "extra_data": {
                        "conversation_id": conversation_id,
                        "session_id": session.session_id,
                        "turn_id": turn_id,
                    }
                },
            )

        except Exception as exc:
            logger.exception(
                "turn_scheduler: turn failed",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            turn_control.settled = True
            error = await self._classify_turn_error(exc)
            await self._publish_turn_error(
                conversation_id,
                session.session_id,
                error,
                turn_id=turn_id,
                system_initiated=system_initiated,
                channel_deliverable=(
                    channel_deliverable or turn_control.absorbed_channel_deliverable
                ),
                delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                delivery_fallback_text=(
                    turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                ),
                chat_mode=resolved_chat_mode.mode,
                chat_mode_source=resolved_chat_mode.source,
                turn_observers=turn_observers,
            )
            TURNS_TOTAL.labels(outcome="error").inc()

        finally:
            duration = asyncio.get_running_loop().time() - start_time
            TURN_DURATION.labels(type=turn_type).observe(duration)
            queued_to_drain: _QueuedMessage | None = None

            if follow_up is not None:
                if turn_succeeded:
                    await self._mark_follow_up_handled(conversation_id, follow_up.follow_up_id)
                else:
                    await self._clear_follow_up_pending(conversation_id, follow_up.follow_up_id)

            absorbed_follow_up_ids = set(turn_control.absorbed_follow_up_ids)
            for follow_up_id in absorbed_follow_up_ids:
                if turn_succeeded:
                    await self._mark_follow_up_handled(conversation_id, follow_up_id)
                else:
                    await self._clear_follow_up_pending(conversation_id, follow_up_id)

            current_task = owner_task or asyncio.current_task()
            async with self._turn_lock(conversation_id):
                registered_active = self._active_turns.get(conversation_id)
                registered_control = self._turn_controls.get(conversation_id)
                active_matches = registered_active is current_task or (
                    owner_task is None and registered_active is None
                )
                control_matches = registered_control is turn_control or (
                    owner_task is None and registered_control is None
                )

                if active_matches:
                    self._active_turns.pop(conversation_id, None)
                if control_matches:
                    self._turn_controls.pop(conversation_id, None)
                    self._turn_sessions.pop(conversation_id, None)
                if (
                    self._pause_waiter.find_pending(
                        pause_type="escalation",
                        conversation_id=conversation_id,
                    )
                    is None
                ):
                    self._escalation_notice_pause_ids.pop(conversation_id, None)
                if not system_initiated:
                    count = self._user_turn_counts.get(user_email, 1)
                    if count <= 1:
                        self._user_turn_counts.pop(user_email, None)
                    else:
                        self._user_turn_counts[user_email] = count - 1

                # Only the task that still owns the active-turn slot may drain
                # the queue.  A stale/cancelled task must never delete a newer
                # task's ownership metadata or launch work in parallel with it.
                queue = self._queued_messages.get(conversation_id)
                if active_matches and control_matches and queue:
                    queued_to_drain = queue.popleft()

            if queued_to_drain is not None:
                queued = queued_to_drain
                try:
                    error = await self.submit_turn(
                        conversation_id,
                        queued.content,
                        user_email=queued.user_email,
                        attachments=queued.attachments,
                        outbound_attachments=queued.outbound_attachments,
                        system_initiated=queued.system_initiated,
                        follow_up=queued.follow_up,
                        channel_deliverable=queued.channel_deliverable,
                        delivery_id=queued.delivery_id,
                        delivery_fallback_text=queued.delivery_fallback_text,
                        turn_observers=queued.turn_observers,
                        client_message_id=queued.client_message_id,
                        queued_message_id=queued.queue_id,
                        prepared_attachment_notice=queued.attachment_notice,
                        prepared_attachment_context=queued.attachment_context,
                        one_shot_chat_mode=queued.one_shot_chat_mode,
                    )
                    await self._notify_queue_updated(
                        conversation_id, turn_observers=queued.turn_observers
                    )
                    if error is not None and queued.follow_up is not None:
                        await self._clear_follow_up_pending(
                            conversation_id, queued.follow_up.follow_up_id
                        )
                except Exception:
                    if queued.follow_up is not None:
                        await self._clear_follow_up_pending(
                            conversation_id, queued.follow_up.follow_up_id
                        )
                    logger.exception(
                        "turn_scheduler: failed to load runtime for queued message",
                        extra={"extra_data": {"conversation_id": conversation_id}},
                    )

    # ------------------------------------------------------------------
    # Observer notification helpers
    # ------------------------------------------------------------------

    def _build_callbacks(
        self,
        conversation_id: str,
        session_id: str,
        message_id: str,
        turn_id: str | None,
        *,
        turn_observers: tuple[TurnObserver, ...] = (),
    ) -> tuple[Any, Any, Any, Any, Any, Any]:
        """Build streaming callbacks that fan out to registered observers."""

        async def on_token(delta: str) -> None:
            chunk_index, content_offset = await self._append_active_stream_chunk(
                conversation_id=conversation_id,
                session_id=session_id,
                message_id=message_id,
                turn_id=turn_id,
                delta=delta,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_token,
                        conversation_id,
                        session_id,
                        message_id,
                        turn_id,
                        delta,
                        chunk_index,
                        content_offset,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
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
                    session_id,
                    message_id=message_id,
                    turn_id=turn_id,
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
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_thinking,
                        conversation_id,
                        session_id,
                        message_id,
                        turn_id,
                        block_id,
                        delta,
                        title,
                        complete,
                        content,
                        started_at,
                        completed_at,
                        duration_ms,
                        source,
                        provider_block_index,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_call(
            tool_name: str,
            call_id: str,
            arguments: dict[str, Any] | None = None,
        ) -> None:
            if tool_name == "delegate" and isinstance(arguments, dict):
                arguments = {
                    "title": arguments.get("title")
                    or arguments.get("task_title")
                    or "Delegated work",
                    "agent_id": arguments.get("agent_id") or arguments.get("used_agent_id"),
                    "wait": bool(arguments.get("wait", False)),
                    "input_redacted": True,
                }
            await self._reset_active_stream(conversation_id)
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_call,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        arguments,
                        turn_id,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_progress(
            call_id: str,
            tool_name: str,
            progress: dict[str, Any],
        ) -> None:
            await self._reset_active_stream(conversation_id)
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_progress,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        progress,
                        turn_id,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                    if hasattr(observer, "on_tool_progress")
                )
            )

        async def on_tool_result(
            call_id: str,
            tool_name: str,
            result: str,
            is_error: bool,
            duration_ms: int | None,
            evaluation: dict[str, Any] | None = None,
            attachments: list[dict[str, Any]] | None = None,
            file_diffs: list[dict[str, Any]] | None = None,
            presentation: dict[str, Any] | None = None,
        ) -> None:
            metadata = (
                presentation.get("tool_output_presentation")
                if isinstance(presentation, dict)
                else None
            )
            if not isinstance(metadata, dict):
                metadata = presentation if isinstance(presentation, dict) else None
            await self._finalize_active_tool_output(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
                result=result,
                is_error=is_error,
                metadata=metadata,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_result,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        result,
                        is_error,
                        duration_ms,
                        evaluation,
                        attachments,
                        file_diffs,
                        turn_id,
                        presentation,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_output_chunk(
            call_id: str,
            tool_name: str,
            delta: str,
            stream: str | None = None,
        ) -> None:
            chunk_index, content_offset = await self._append_active_tool_output_chunk(
                conversation_id=conversation_id,
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                turn_id=turn_id,
                delta=delta,
                stream=stream,
            )
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_tool_output_chunk,
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        delta,
                        stream,
                        turn_id,
                        chunk_index,
                        content_offset,
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        return (
            on_token,
            on_thinking,
            on_tool_call,
            on_tool_result,
            on_tool_progress,
            on_tool_output_chunk,
        )

    def _iter_observers(
        self,
        conversation_id: str,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> list[TurnObserver]:
        observers: list[TurnObserver] = list(self._observers.get(conversation_id, []))
        for observer in turn_observers or ():
            if (
                observer not in observers
                and (conversation_id, id(observer)) not in self._disabled_observers
            ):
                observers.append(observer)
        return [
            observer
            for observer in observers
            if (conversation_id, id(observer)) not in self._disabled_observers
        ]

    async def _notify_observers(
        self,
        conversation_id: str,
        method: str,
        *args: Any,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Call a method on all observers for a conversation."""
        await asyncio.gather(
            *(
                self._call_observer(conversation_id, observer, getattr(observer, method), *args)
                for observer in self._iter_observers(conversation_id, turn_observers=turn_observers)
            )
        )

    async def _notify_observers_system_message(
        self,
        conversation_id: str,
        text: str,
        *,
        notice_id: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        turn_id: str | None = None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Send a system message to all observers."""
        await self._notify_observers(
            conversation_id,
            "on_system_message",
            conversation_id,
            text,
            notice_id,
            kind,
            scope,
            turn_id,
            turn_observers=turn_observers,
        )

    async def _publish_turn_completed(
        self,
        result: TurnResult,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Notify observers and publish lifecycle event."""
        await self._reset_active_stream(result.conversation_id)
        self._settle_turn_waiters(result.conversation_id, result)
        await asyncio.gather(
            *(
                self._call_observer(
                    result.conversation_id,
                    observer,
                    observer.on_turn_complete,
                    result,
                )
                for observer in self._iter_observers(
                    result.conversation_id,
                    turn_observers=turn_observers,
                )
            )
        )

        await self._event_bus.publish(
            Event(
                type=EventType.TURN_COMPLETED,
                data={
                    "conversation_id": result.conversation_id,
                    "session_id": result.session_id,
                    "message_id": result.message_id,
                    "turn_id": result.turn_id,
                    "last_seq": result.last_seq,
                    "context_usage": result.context_usage,
                    "delegated": result.delegated,
                    "task_id": result.task_id,
                    "title_changed": result.title_changed,
                    "new_title": result.new_title,
                    "queued_count": self.queued_count(result.conversation_id),
                    "system_initiated": result.system_initiated,
                    "channel_deliverable": result.channel_deliverable,
                    "delivery_id": result.delivery_id,
                    "delivery_fallback_text": result.delivery_fallback_text,
                    "final_content": result.final_content,
                    "completed_at": (
                        result.completed_at.isoformat()
                        if result.completed_at is not None
                        else datetime.now(UTC).isoformat()
                    ),
                    "chat_mode": result.chat_mode,
                    "chat_mode_source": result.chat_mode_source,
                    "attachments": strip_attachment_payload_bytes(result.attachments or []),
                    "partial": result.partial,
                    "finish_reason": result.finish_reason,
                },
            )
        )
        await self._notify_managed_turn_result(result)

    async def _notify_managed_turn_result(self, result: TurnResult) -> None:
        """Update managed-conversation state for a completed scheduler turn."""

        cancelled_partial = result.partial and result.finish_reason == "user_cancelled"
        if cancelled_partial:
            await self._notify_managed_conversation_controller(
                target_conversation_id=result.conversation_id,
                target_session_id=result.session_id,
                turn_state="interrupted",
                conversation_state="open",
                status=FollowUpStatus.CANCELLED,
                summary=result.final_content,
                turn_id=result.turn_id,
                error_message="The current turn was cancelled.",
                recoverable=True,
                clear_active_turn=True,
                notify_on_completion=True,
                completed=False,
            )
            return

        await self._notify_managed_conversation_controller(
            target_conversation_id=result.conversation_id,
            target_session_id=result.session_id,
            turn_state="running" if result.managed_continuation_pending else "completed",
            conversation_state="open" if result.managed_continuation_pending else "completed",
            status=FollowUpStatus.COMPLETED,
            summary=result.final_content,
            turn_id=result.turn_id,
            clear_active_turn=not result.managed_continuation_pending,
            notify_on_completion=not result.managed_continuation_pending,
            completed=not result.managed_continuation_pending,
        )

    async def _publish_turn_error(
        self,
        conversation_id: str,
        session_id: str,
        error: TurnError,
        *,
        turn_id: str | None = None,
        system_initiated: bool = False,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        chat_mode: ChatMode = "default",
        chat_mode_source: str = "system_default",
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Notify observers and publish lifecycle event."""
        await self._reset_active_stream(conversation_id)
        self._settle_turn_waiters(conversation_id, error)
        await asyncio.gather(
            *(
                self._call_observer(
                    conversation_id,
                    observer,
                    observer.on_turn_error,
                    conversation_id,
                    error,
                )
                for observer in self._iter_observers(
                    conversation_id,
                    turn_observers=turn_observers,
                )
            )
        )

        await self._event_bus.publish(
            Event(
                type=EventType.TURN_ERROR,
                data={
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "chat_mode": chat_mode,
                    "chat_mode_source": chat_mode_source,
                    "error_code": error.code,
                    "error_message": error.message,
                    "recoverable": error.recoverable,
                    "system_initiated": system_initiated,
                    "channel_deliverable": channel_deliverable,
                    "delivery_id": delivery_id,
                    "delivery_fallback_text": delivery_fallback_text,
                },
            )
        )
        await self._notify_managed_turn_error(
            conversation_id=conversation_id,
            session_id=session_id,
            error=error,
            turn_id=turn_id,
        )

    async def _notify_managed_turn_error(
        self,
        *,
        conversation_id: str,
        session_id: str,
        error: TurnError,
        turn_id: str | None,
    ) -> None:
        """Update managed-conversation state for a failed scheduler turn."""

        interrupted = error.code in _CANCELLED_TURN_ERROR_CODES
        await self._notify_managed_conversation_controller(
            target_conversation_id=conversation_id,
            target_session_id=session_id,
            turn_state="interrupted" if interrupted else "failed",
            conversation_state="open",
            status=FollowUpStatus.CANCELLED if interrupted else FollowUpStatus.FAILED,
            summary=error.message,
            turn_id=turn_id,
            error_message=error.message,
            recoverable=error.recoverable,
        )

    def _settle_turn_waiters(
        self,
        conversation_id: str,
        value: TurnResult | TurnError,
    ) -> None:
        """Resolve futures waiting for the next visible turn settlement."""

        waiters = self._turn_waiters.pop(conversation_id, [])
        for future in waiters:
            if not future.done():
                future.set_result(value)

    async def _notify_managed_conversation_controller(
        self,
        *,
        target_conversation_id: str,
        target_session_id: str,
        turn_state: str,
        conversation_state: str,
        status: FollowUpStatus,
        summary: str | None,
        turn_id: str | None,
        error_message: str | None = None,
        recoverable: bool | None = None,
        clear_active_turn: bool = True,
        notify_on_completion: bool = True,
        completed: bool | None = None,
    ) -> None:
        """Update managed-conversation state and notify the controller when requested."""

        try:
            async with self._session_factory() as db_session:
                link = await queries.get_managed_conversation_link_for_target(
                    db_session,
                    target_conversation_id,
                )
                if link is None:
                    return
                if link.active_turn_id and turn_id and link.active_turn_id != turn_id:
                    # A stale completion from an interrupted/cancelled previous turn must not
                    # clear notification state for a newer controller-submitted turn.
                    return
                notify = bool(link.notify_on_completion and notify_on_completion)
                await queries.update_managed_conversation_link(
                    db_session,
                    link.link_id,
                    conversation_state=conversation_state,
                    turn_state=turn_state,
                    target_session_id=target_session_id,
                    active_turn_id=turn_id if not clear_active_turn else None,
                    clear_active_turn_id=clear_active_turn,
                    notify_on_completion=False if notify_on_completion else None,
                    last_result_summary=summary,
                    last_error=error_message,
                    completed=completed
                    if completed is not None
                    else conversation_state == "completed",
                )
                await queries.mark_conversation_read(db_session, target_conversation_id)
                await db_session.commit()
                if not notify:
                    return
                needs_attention = status in {FollowUpStatus.FAILED, FollowUpStatus.CANCELLED}
                if error_message and summary and error_message not in summary:
                    raw_summary = f"{error_message}\n\nPartial output:\n{summary}"
                else:
                    raw_summary = error_message or summary
                follow_up_summary = truncate_follow_up_text(raw_summary, max_chars=600)
                if needs_attention:
                    description = (
                        "A managed agent work turn needs attention. "
                        f"Status: {turn_state}. "
                        "Review the managed conversation, then resume with "
                        "agent_conversation_retry if the last turn is recoverable, or continue "
                        "with agent_conversation_send when new instructions are needed."
                    )
                    title = f"Agent work needs attention: {link.title or target_conversation_id}"
                else:
                    description = (
                        "An agent work turn finished. Integrate the result, "
                        "ask the user if clarification is needed, or continue the managed "
                        "conversation with agent_conversation_send."
                    )
                    title = f"Agent work finished: {link.title or target_conversation_id}"
                follow_up = {
                    "version": 1,
                    "follow_up_id": build_follow_up_id(
                        kind="managed_conversation",
                        conversation_id=link.controller_conversation_id,
                        parts={
                            "link_id": link.link_id,
                            "target_conversation_id": target_conversation_id,
                            "turn_id": turn_id,
                            "status": status.value,
                        },
                    ),
                    "mode": FollowUpMode.INTEGRATE.value,
                    "origin_kind": FollowUpOriginKind.OTHER.value,
                    "relevance_hint": FollowUpRelevanceHint.SAME_THREAD.value,
                    "required_action": (
                        FollowUpRequiredAction.INFORM_FAILURE.value
                        if needs_attention
                        else FollowUpRequiredAction.INTEGRATE_RESULT.value
                    ),
                    "topic_ref": target_conversation_id,
                    "status": status.value,
                    "title": title,
                    "summary": follow_up_summary,
                    "description": description,
                    "metadata": {
                        "link_id": link.link_id,
                        "target_agent_id": link.target_agent_id,
                        "target_conversation_id": target_conversation_id,
                        "target_session_id": target_session_id,
                        "turn_state": turn_state,
                        "recoverable": recoverable,
                    },
                }
            await self._event_bus.publish(
                Event(
                    type=EventType.FOLLOW_UP_TURN_REQUESTED,
                    data={
                        "conversation_id": link.controller_conversation_id,
                        "follow_up": follow_up,
                    },
                )
            )
        except Exception:
            logger.warning(
                "turn_scheduler: failed to notify managed conversation controller",
                extra={"extra_data": {"target_conversation_id": target_conversation_id}},
                exc_info=True,
            )

    async def _call_observer(
        self,
        conversation_id: str,
        observer: TurnObserver,
        callback: Any,
        *args: Any,
    ) -> None:
        try:
            awaitable = callback(*args)
        except TypeError:
            trimmed_args = _trim_callback_args(callback, args)
            if len(trimmed_args) == len(args):
                raise
            awaitable = callback(*trimmed_args)
        try:
            await asyncio.wait_for(awaitable, timeout=1.0)
        except Exception:
            key = (conversation_id, id(observer))
            self._observer_failures[key] += 1
            if self._observer_failures[key] >= 3:
                logger.warning(
                    "turn_scheduler: removing unstable observer",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                    exc_info=True,
                )
                self.remove_observer(conversation_id, observer)
                self._disabled_observers.add(key)
            else:
                logger.debug(
                    "turn_scheduler: observer callback failed",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "failure_count": self._observer_failures[key],
                        }
                    },
                    exc_info=True,
                )
        else:
            self._observer_failures.pop((conversation_id, id(observer)), None)

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    async def _classify_turn_error(self, error: Exception) -> TurnError:
        """Classify a turn error into a structured TurnError."""
        return await classify_turn_error(self._providers, error)

    # ------------------------------------------------------------------
    # Conversation runtime loading
    # ------------------------------------------------------------------

    async def _load_conversation_runtime(
        self,
        conversation_id: str,
        *,
        user_message: str | None = None,
    ) -> tuple[ConversationModel, SessionModel, AgentDefinition, bool] | None:
        """Load conversation, session, and agent for a turn.

        Handles deferred session creation after compaction and brand-new
        conversations without a session.
        """
        from cognis.api.serializers import agent_to_response
        from cognis.core.session import _to_conversation_model, _to_session_model
        from cognis.store.queries import (
            get_agent,
            get_conversation,
            get_session_row,
            update_conversation_active_session,
        )

        async with self._session_factory() as session:
            conversation_row = await get_conversation(session, conversation_id)
            if conversation_row is None:
                return None
            agent_row = await get_agent(session, conversation_row.agent_id)
            if agent_row is None:
                return None
            agent_model = AgentDefinition.model_validate(agent_to_response(agent_row).model_dump())
            conversation_model = _to_conversation_model(conversation_row)

            if conversation_row.active_session_id is None:
                lock = self._deferred_creation_locks.setdefault(conversation_id, asyncio.Lock())
                async with lock:
                    async with self._session_factory() as db_session_check:
                        conv_row_check = await get_conversation(db_session_check, conversation_id)
                        if conv_row_check is None:
                            return None
                        if conv_row_check.active_session_id is not None:
                            new_row = await get_session_row(
                                db_session_check, conv_row_check.active_session_id
                            )
                            if new_row is None:
                                return None
                            conversation_model.active_session_id = conv_row_check.active_session_id
                            return (
                                conversation_model,
                                _to_session_model(new_row),
                                agent_model,
                                _should_bootstrap_wait_for_intention(conversation_model),
                            )

                    intention = user_message or f"Conversation with {agent_row.name}"
                    try:
                        root_session = await self._session_manager.ensure_root_session(
                            conversation_id=conversation_row.conversation_id,
                            user_email=conversation_row.user_email,
                            agent_id=conversation_row.agent_id,
                            intention=intention,
                        )
                    except Exception as exc:
                        raise SessionCreationFailedError("Could not create a session") from exc
                    conversation_model.active_session_id = root_session.session_id
                    return conversation_model, root_session, agent_model, True

            session_row = await get_session_row(session, conversation_row.active_session_id)
            if session_row is None and conversation_row.status == "active":
                await update_conversation_active_session(
                    session, conversation_row.conversation_id, None
                )
                await session.commit()
                conversation_model.active_session_id = None

        if session_row is None:
            if (
                conversation_model.status == "active"
                and conversation_model.active_session_id is None
            ):
                return await self._load_conversation_runtime(
                    conversation_id,
                    user_message=user_message,
                )
            return None

        session_model = _to_session_model(session_row)

        # --- Deferred session creation after /compact ---
        if (
            session_model.status == SessionStatus.COMPLETED
            and session_model.completion_reason == "compacted"
        ):
            lock = self._deferred_creation_locks.setdefault(conversation_id, asyncio.Lock())
            async with lock:
                # Double-check: re-read after acquiring lock
                async with self._session_factory() as db_session_check:
                    conv_row_check = await get_conversation(db_session_check, conversation_id)
                if (
                    conv_row_check is not None
                    and conv_row_check.active_session_id != session_model.session_id
                ):
                    # Another caller already rotated
                    async with self._session_factory() as db_session_reload:
                        new_row = await get_session_row(
                            db_session_reload, conv_row_check.active_session_id
                        )
                    if new_row is not None:
                        conversation_model.active_session_id = conv_row_check.active_session_id
                        return (
                            conversation_model,
                            _to_session_model(new_row),
                            agent_model,
                            _should_bootstrap_wait_for_intention(conversation_model),
                        )

                compaction_summary, tail_start_seq = await self._read_compaction_metadata(
                    session_model
                )
                tail_events = (
                    await self._read_tail_events(session_model, tail_start_seq)
                    if tail_start_seq is not None
                    else None
                )
                intention = user_message[:200] if user_message else "Continued conversation"
                if compaction_summary:
                    intention = f"Continuation: {intention}"
                try:
                    new_session = await self._session_manager.rotate_session(
                        conversation_id=conversation_model.conversation_id,
                        current_session=session_model,
                        intention=intention,
                        completion_reason="compacted",
                        compaction_summary=compaction_summary,
                        tail_events=tail_events,
                    )
                    ROTATION_TOTAL.labels(trigger="deferred").inc()
                    if tail_events:
                        from cognis.core.compaction import COMPACTION_DEFERRED_TAIL_SEEDED

                        COMPACTION_DEFERRED_TAIL_SEEDED.inc(len(tail_events))
                except Exception as exc:
                    raise SessionCreationFailedError(
                        "Could not create session after compaction"
                    ) from exc
                conversation_model.active_session_id = new_session.session_id

                # Pre-populate session cache — rotate_session already seeds tail
                # events via _seed_rotated_tail_events, so we only need a refresh
                # here (no manual apply_compaction with compaction_seq=0).
                if compaction_summary:
                    await self._session_cache.refresh(new_session)

                logger.info(
                    "turn_scheduler: deferred session created after compaction",
                    extra={
                        "extra_data": {
                            "conversation_id": conversation_id,
                            "old_session_id": session_model.session_id,
                            "new_session_id": new_session.session_id,
                        }
                    },
                )
                return (
                    conversation_model,
                    new_session,
                    agent_model,
                    _should_bootstrap_wait_for_intention(conversation_model),
                )

        # Periodic cleanup of deferred creation locks (outside the lock block)
        if len(self._deferred_creation_locks) > _MAX_DEFERRED_LOCKS:
            to_remove = [
                cid for cid, lk in self._deferred_creation_locks.items() if not lk.locked()
            ]
            for cid in to_remove:
                self._deferred_creation_locks.pop(cid, None)

        return (
            conversation_model,
            session_model,
            agent_model,
            _should_bootstrap_wait_for_intention(conversation_model),
        )

    async def _read_compaction_metadata(
        self, session: SessionModel
    ) -> tuple[str | None, int | None]:
        """Read the last compaction_summary event from a completed session.

        Returns ``(summary, tail_start_seq)`` so the deferred-rotation path
        can re-fetch the preserved tail events and seed them into the new
        session via ``rotate_session(tail_events=...)``.
        """
        try:
            result = await self._providers.guardrails.read_events(
                session_id=session.intaris_session_id or session.session_id,
                after_seq=0,
                allow_missing_stream=True,
            )
            for event in reversed(result.events):
                if event.get("type") == "compaction_summary":
                    data = event.get("data", {})
                    return data.get("summary"), data.get("tail_start_seq")
        except Exception:
            logger.warning(
                "turn_scheduler: failed to read compaction metadata",
                extra={"extra_data": {"session_id": session.session_id}},
            )
        return None, None

    async def _read_tail_events(
        self, session: SessionModel, tail_start_seq: int
    ) -> list[Any] | None:
        """Fetch the preserved tail events from the old session's Intaris stream.

        These are the events that were kept verbatim after compaction.  We
        re-fetch them so ``rotate_session`` can seed them into the new session
        via ``_seed_rotated_tail_events``, restoring continuity that would
        otherwise be lost in the deferred-rotation path.

        Returns a list of simple namespace objects with ``.type``, ``.data``,
        and ``.seq`` attributes, matching what ``_seed_rotated_tail_events``
        expects (it uses ``getattr`` for all three fields).
        """
        # Content event types to carry forward; skip system/compaction markers.
        _TAIL_EVENT_TYPES = {
            "user_message",
            "assistant_message",
            "assistant_thinking",
            "tool_call",
            "tool_result",
            "delegation",
        }

        class _TailEvent:
            """Lightweight wrapper so _seed_rotated_tail_events can use getattr."""

            __slots__ = ("type", "data", "seq")

            def __init__(self, etype: str, data: dict[str, Any], seq: int | None) -> None:
                self.type = etype
                self.data = data
                self.seq = seq

        try:
            result = await self._providers.guardrails.read_events(
                session_id=session.intaris_session_id or session.session_id,
                after_seq=tail_start_seq - 1,
                allow_missing_stream=True,
            )
            wrapped = [
                _TailEvent(
                    etype=e["type"],
                    data=e.get("data", {}),
                    seq=e.get("seq"),
                )
                for e in result.events
                if isinstance(e, dict) and e.get("type") in _TAIL_EVENT_TYPES
            ]
            return wrapped or None
        except Exception:
            logger.warning(
                "turn_scheduler: failed to read tail events for deferred rotation",
                extra={
                    "extra_data": {
                        "session_id": session.session_id,
                        "tail_start_seq": tail_start_seq,
                    }
                },
            )
            return None

    async def _touch_conversation(
        self, conversation_id: str, *, when: datetime | None = None
    ) -> None:
        """Update last_message_at on the conversation for unread tracking."""
        try:
            async with self._session_factory() as db_session:
                row = await queries.get_conversation(db_session, conversation_id)
                if row is not None:
                    timestamp = when or datetime.now(UTC)
                    row.last_message_at = timestamp
                    row.updated_at = timestamp
                    await db_session.commit()
        except Exception:
            logger.warning(
                "turn_scheduler: failed to update last_message_at",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )


# ---------------------------------------------------------------------------
# Error classification (standalone for testability)
# ---------------------------------------------------------------------------


async def classify_turn_error(providers: Any, error: Exception) -> TurnError:
    """Classify a turn error into a structured TurnError.

    This is a standalone function (not a method) so it can be tested
    independently without constructing a full TurnScheduler.
    """
    lowered = str(error).lower()
    safe_detail = sanitize_client_error_detail(error, fallback="request failed")

    if isinstance(error, SessionCreationFailedError):
        return TurnError(
            code="session_creation_failed",
            message="Could not create a session. Try again or check the diagnostics page.",
            recoverable=True,
            detail={"error_detail": safe_detail},
        )
    if isinstance(error, ImmutablePrefixUnavailable):
        return TurnError(
            code="immutable_prefix_unavailable",
            message="Immutable prefix is unavailable for this session.",
            recoverable=False,
            detail={"error_detail": safe_detail, "reason": error.reason},
        )
    if isinstance(error, ValueError) and "no llm model configured" in lowered:
        return TurnError(
            code="provider_not_configured:llm",
            message="No LLM provider is configured. Go to Settings > Providers to add one.",
            recoverable=True,
            detail={"error_detail": safe_detail},
        )

    provider_checks: list[tuple[str, Any]] = []
    for provider_name in ("guardrails", "llm", "memory"):
        provider = getattr(providers, provider_name, None)
        if provider is None:
            continue
        try:
            provider_checks.append((provider_name, await provider.health()))
        except Exception:
            continue

    for provider_name, health in provider_checks:
        if health.status == "healthy":
            continue
        if provider_name == "guardrails":
            return TurnError(
                code="provider_unreachable:guardrails",
                message="Guardrails service is unreachable — tool calls are blocked until it recovers. Check that Intaris is running.",
                recoverable=True,
                detail={"error_detail": safe_detail},
            )
        if provider_name == "llm":
            if "no llm model configured" in lowered or "not configured" in lowered:
                return TurnError(
                    code="provider_not_configured:llm",
                    message="No LLM provider is configured. Go to Settings > Providers to add one.",
                    recoverable=True,
                    detail={"error_detail": safe_detail},
                )
            return TurnError(
                code="provider_error:llm",
                message="LLM provider returned an error. Check your provider configuration in Settings.",
                recoverable=True,
                detail={"error_detail": safe_detail},
            )
        if provider_name == "memory":
            return TurnError(
                code="provider_unreachable:memory",
                message="Memory is currently unavailable — this conversation won't have access to past context.",
                recoverable=True,
                detail={"error_detail": safe_detail},
            )

    if isinstance(error, (httpx.HTTPError, TimeoutError)):
        return TurnError(
            code="provider_error:llm",
            message="A provider request failed while processing this turn.",
            recoverable=True,
            detail={"error_detail": safe_detail},
        )

    return TurnError(
        code="turn_failed",
        message="Turn execution failed.",
        recoverable=True,
        detail={"error_detail": safe_detail},
    )


# ---------------------------------------------------------------------------
# Follow-up prompt builder
# ---------------------------------------------------------------------------


def _build_follow_up_prompt(
    status: str | None,
    *,
    task_id: str | None = None,
    task_title: str | None = None,
    result_summary: str | None = None,
    description: str | None = None,
    source_type: str | None = None,
    gate_message: str | None = None,
    gate_options: list[dict[str, Any]] | None = None,
) -> str:
    """Build a system prompt for the follow-up turn after a task/delegation completes.

    The prompt provides facts about the completed task and lets the LLM
    decide how to present the result based on the agent's personality and
    the user's preferences (which are already in context via Mnemory).
    """
    status_name = (status or "updated").lower()

    # Task-specific prompts (from workflow engine)
    if task_id:
        is_scheduled = source_type == "scheduler"
        prefix = "Scheduled task" if is_scheduled else "Background task"
        title_str = f'"{task_title}"' if task_title else task_id

        if status_name == "completed":
            lines = [f"{prefix} {title_str} (task_id: {task_id}) has completed."]
            if is_scheduled:
                lines.append("This task runs on a recurring schedule.")
            if description:
                lines.append(f"\nTask description: {description}")
            if result_summary:
                lines.append(f"\nResult summary: {result_summary}")
            lines.append(
                "\nDecide how to handle this result based on the user's preferences "
                "and the context. You may present the summary directly if it is "
                "sufficient, or use the get_task_output tool with "
                f'task_id="{task_id}" to retrieve the full output first if you '
                "need more detail for a complete response."
            )
            return "\n".join(lines)

        if status_name == "failed":
            lines = [f"{prefix} {title_str} (task_id: {task_id}) has failed."]
            if description:
                lines.append(f"\nTask description: {description}")
            if result_summary:
                lines.append(f"\nError details: {result_summary}")
            lines.append(
                "\nInform the user about the failure. Do not attempt to retry "
                "or recreate the task automatically — let the user decide how "
                "to proceed."
            )
            return "\n".join(lines)

        if status_name == "cancelled":
            return (
                f"{prefix} {title_str} (task_id: {task_id}) was cancelled. "
                "Provide a brief follow-up to the user if warranted."
            )

        if status_name == "paused":
            lines = [f"{prefix} {title_str} (task_id: {task_id}) needs your attention."]
            if gate_message:
                lines.append(f"\nReason: {gate_message}")
            if gate_options:
                option_labels = [
                    opt.get("label", opt.get("action", "?"))
                    for opt in gate_options
                    if isinstance(opt, dict)
                ]
                if option_labels:
                    lines.append(f"Available actions: {', '.join(option_labels)}")
            lines.append(
                "\nExplain to the user why the task paused and what their options "
                "are. If the task exhausted its retry attempts, explain what went "
                "wrong. The user can resolve the paused gate with `resolve_task_pause` "
                "(retry, continue, or cancel). Do NOT choose automatically — let the "
                "user decide."
            )
            return "\n".join(lines)

        # Generic task update
        return (
            f"{prefix} {title_str} (task_id: {task_id}) status: {status_name}. "
            f"Summary: {result_summary or 'No summary available.'}."
        )

    # Delegation-specific prompts (from agent_loop async delegations)
    if status_name == "failed":
        return (
            "A delegated sub-session has failed. "
            "Review the recent delegation_failed event in the session history "
            "and provide a concise user-facing follow-up."
        )
    if status_name == "completed":
        return (
            "A delegated sub-session has completed. "
            "Review the recent delegation_completed event in the session history "
            "and present the result to the user."
        )
    return (
        "A background operation has completed. "
        "Review the recent events in the session history and provide a concise follow-up."
    )
