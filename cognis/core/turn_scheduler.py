"""Transport-agnostic turn orchestration.

TurnScheduler owns the full lifecycle of a chat turn — from user message
to response — without any dependency on WebSocket or other transport layers.

It handles:
- Turn submission and serialization (one active turn per conversation)
- Decision engine dispatch (inline vs delegate)
- Workflow selection for delegated tasks
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
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx
from prometheus_client import Counter, Histogram

from cognis.api.error_sanitizer import sanitize_client_error_detail
from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.session import BLOCKED_STATES, ConversationModel, SessionModel, SessionStatus
from cognis.models.task import TaskDelivery
from cognis.runtime_context import current_agent_id, current_user_email

logger = get_logger(__name__)

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_QUEUED_MESSAGES = 5
DEFAULT_TURN_LIMIT = 3
_MAX_DEFERRED_LOCKS = 200


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
    last_seq: int = 0
    context_usage: dict[str, Any] | None = None
    delegated: bool = False
    task_id: str | None = None
    error: TurnError | None = None
    title_changed: bool = False
    new_title: str | None = None


@dataclass(slots=True)
class _QueuedMessage:
    """A message queued behind an active turn."""

    content: str
    user_email: str
    attachments: list[dict[str, Any]] | None = None
    system_initiated: bool = False


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
        delta: str,
    ) -> None: ...

    async def on_tool_call(
        self,
        conversation_id: str,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
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
    ) -> None: ...

    async def on_turn_complete(self, result: TurnResult) -> None: ...

    async def on_turn_error(self, conversation_id: str, error: TurnError) -> None: ...

    async def on_system_message(self, conversation_id: str, text: str) -> None: ...

    async def on_queued(self, conversation_id: str, queued_count: int) -> None: ...


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

        # Per-conversation turn serialization
        self._active_turns: dict[str, asyncio.Task[None]] = {}
        self._turn_controls: dict[str, asyncio.Event] = {}
        self._turn_sessions: dict[str, str] = {}
        self._queued_messages: dict[str, deque[_QueuedMessage]] = defaultdict(deque)

        # Per-user concurrent turn limit
        self._user_turn_counts: dict[str, int] = defaultdict(int)

        # Per-conversation observers (multiple allowed — e.g. multiple browser tabs)
        self._observers: dict[str, list[TurnObserver]] = defaultdict(list)

        # Per-conversation session creation locks (bootstrap + compaction recovery)
        self._deferred_creation_locks: dict[str, asyncio.Lock] = {}

        # Register for follow-up turn events
        event_bus.subscribe(EventType.FOLLOW_UP_TURN_REQUESTED, self._handle_follow_up_event)
        logger.info("turn_scheduler: registered on EventBus")

    # ------------------------------------------------------------------
    # Observer management
    # ------------------------------------------------------------------

    def add_observer(self, conversation_id: str, observer: TurnObserver) -> None:
        """Register a streaming observer for a conversation."""
        self._observers[conversation_id].append(observer)

    def remove_observer(self, conversation_id: str, observer: TurnObserver) -> None:
        """Remove a streaming observer for a conversation."""
        observers = self._observers.get(conversation_id)
        if observers:
            with contextlib.suppress(ValueError):
                observers.remove(observer)
            if not observers:
                del self._observers[conversation_id]

    def remove_all_observers(self, observer: TurnObserver) -> None:
        """Remove an observer from all conversations (e.g. on disconnect)."""
        empty_keys: list[str] = []
        for cid, observers in self._observers.items():
            with contextlib.suppress(ValueError):
                observers.remove(observer)
            if not observers:
                empty_keys.append(cid)
        for cid in empty_keys:
            del self._observers[cid]

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
        system_initiated: bool = False,
    ) -> TurnError | None:
        """Submit a chat turn for execution.

        Returns a ``TurnError`` immediately if the turn cannot be started
        (authorization failure, session blocked, escalation pending, etc.).
        Returns ``None`` on successful submission — results are delivered
        via ``TurnObserver`` callbacks and EventBus lifecycle events.

        Turns are serialized per conversation. If a turn is already active,
        the message is queued (up to ``MAX_QUEUED_MESSAGES``).
        """
        normalized_attachments, attachment_error = await self._resolve_attachments_for_turn(
            user_email=user_email,
            attachments=attachments or [],
        )
        if attachment_error is not None:
            return attachment_error

        # Load conversation runtime only after validating attachments so
        # failed first sends do not bootstrap a session unnecessarily.
        try:
            runtime = await self._load_conversation_runtime(conversation_id, user_message=content)
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

        # Authorization check
        if not system_initiated and conversation.user_email != user_email:
            return TurnError(
                code="forbidden",
                message="Conversation access denied",
                recoverable=False,
            )

        attachment_notice = await self._build_attachment_notice(
            session=session,
            agent=agent,
            attachments=normalized_attachments,
        )

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
                # Queue the message behind the escalation
                self._queued_messages[conversation_id].append(
                    _QueuedMessage(
                        content=content,
                        user_email=user_email,
                        attachments=[
                            item.model_dump(mode="json") for item in normalized_attachments
                        ],
                    )
                )
                await self._notify_observers_system_message(
                    conversation_id,
                    "Waiting for escalation resolution. "
                    "Use /approve or /deny, or use the buttons above.",
                )
                return None

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

        # Per-user concurrent turn limit
        if not system_initiated:
            user_active = self._user_turn_counts.get(user_email, 0)
            if user_active >= DEFAULT_TURN_LIMIT:
                return TurnError(
                    code="rate_limited",
                    message="Too many concurrent turns. Wait for a turn to finish.",
                    recoverable=True,
                )

        # Queue if a turn is already active
        active = self._active_turns.get(conversation_id)
        if active is not None and not active.done():
            queue = self._queued_messages[conversation_id]
            if len(queue) >= MAX_QUEUED_MESSAGES:
                return TurnError(
                    code="queue_full",
                    message="Message queue is full. Wait for the current turn to finish.",
                    recoverable=True,
                )
            queue.append(
                _QueuedMessage(
                    content=content,
                    user_email=user_email,
                    attachments=[item.model_dump(mode="json") for item in normalized_attachments],
                    system_initiated=system_initiated,
                )
            )
            # Notify observers that the message was queued
            for observer in list(self._observers.get(conversation_id, [])):
                with contextlib.suppress(Exception):
                    await observer.on_queued(conversation_id, len(queue))
            return None

        # Launch the turn
        self._launch_turn(
            conversation=conversation,
            session=session,
            agent=agent,
            content=content,
            user_email=user_email,
            attachments=normalized_attachments,
            attachment_notice=attachment_notice,
            system_initiated=system_initiated,
            bootstrap_wait_for_intention=bootstrap_wait_for_intention,
        )
        return None

    async def cancel_turn(self, conversation_id: str) -> bool:
        """Cancel the active turn and all its child sub-sessions."""
        control = self._turn_controls.get(conversation_id)
        if control is None:
            return False
        control.set()
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

    def queued_count(self, conversation_id: str) -> int:
        """Return the number of queued messages for a conversation."""
        return len(self._queued_messages.get(conversation_id, []))

    # ------------------------------------------------------------------
    # Follow-up turn handling (EventBus subscriber)
    # ------------------------------------------------------------------

    async def _handle_follow_up_event(self, event: Event) -> None:
        """Handle a FOLLOW_UP_TURN_REQUESTED event."""
        conversation_id = event.data.get("conversation_id")
        if not isinstance(conversation_id, str):
            logger.warning("turn_scheduler: follow-up event missing conversation_id, dropping")
            return

        prompt = _build_follow_up_prompt(
            event.data.get("status") if isinstance(event.data.get("status"), str) else None,
            task_id=event.data.get("task_id")
            if isinstance(event.data.get("task_id"), str)
            else None,
            task_title=event.data.get("task_title")
            if isinstance(event.data.get("task_title"), str)
            else None,
            result_summary=event.data.get("result_summary")
            if isinstance(event.data.get("result_summary"), str)
            else None,
            gate_message=event.data.get("gate_message")
            if isinstance(event.data.get("gate_message"), str)
            else None,
            gate_options=event.data.get("gate_options")
            if isinstance(event.data.get("gate_options"), list)
            else None,
        )

        # Use submit_turn for unified serialization
        # Determine user_email from the conversation
        async with self._session_factory() as db_session:
            from cognis.store.queries import get_conversation

            row = await get_conversation(db_session, conversation_id)
        if row is None:
            logger.warning(
                "turn_scheduler: follow-up conversation not found",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            return

        await self.submit_turn(
            conversation_id,
            prompt,
            user_email=row.user_email,
            system_initiated=True,
        )

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
                url = await self._artifact_store.async_get_signed_url(
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

    async def _build_attachment_notice(
        self,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        attachments: list[AttachmentRef],
    ) -> str | None:
        if not attachments:
            return None
        explicit_model = self._session_cache.get_model_override(session.session_id) or (
            agent.llm_config.model if agent.llm_config else None
        )
        resolved_model = await self._providers.llm.resolve_model(
            explicit_model=explicit_model,
            task_type="default",
        )
        model_info = await self._providers.llm.get_model_info(resolved_model)
        unsupported: list[str] = []
        for attachment in attachments:
            if attachment.kind == ArtifactKind.IMAGE and model_info.supports_vision:
                continue
            if attachment.kind == ArtifactKind.PDF and model_info.supports_pdf_input:
                continue
            if attachment.kind == ArtifactKind.AUDIO and model_info.supports_audio_input:
                continue
            if attachment.kind == ArtifactKind.FILE and model_info.supports_file_input:
                continue
            unsupported.append(f"{attachment.filename} ({attachment.kind.value})")

        if not unsupported:
            return None
        joined = ", ".join(unsupported)
        return (
            f"The current model ({resolved_model}) cannot read these attachments natively: {joined}. "
            "You must explicitly refuse to analyze those files and ask the user to switch to a compatible model if needed."
        )

    def _launch_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        content: str,
        user_email: str,
        attachments: list[AttachmentRef] | None = None,
        attachment_notice: str | None = None,
        system_initiated: bool = False,
        bootstrap_wait_for_intention: bool = False,
    ) -> None:
        """Launch a turn as a background asyncio.Task."""
        conversation_id = conversation.conversation_id
        control = asyncio.Event()
        self._turn_controls[conversation_id] = control
        self._turn_sessions[conversation_id] = session.session_id
        if not system_initiated:
            self._user_turn_counts[user_email] = self._user_turn_counts.get(user_email, 0) + 1
        self._active_turns[conversation_id] = asyncio.create_task(
            self._run_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                content=content,
                user_email=user_email,
                attachments=attachments,
                attachment_notice=attachment_notice,
                system_initiated=system_initiated,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                cancel_event=control,
            )
        )

    async def _run_turn(
        self,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        content: str,
        user_email: str,
        attachments: list[AttachmentRef] | None,
        attachment_notice: str | None,
        system_initiated: bool,
        bootstrap_wait_for_intention: bool,
        cancel_event: asyncio.Event,
    ) -> None:
        """Execute a single chat turn."""
        conversation_id = conversation.conversation_id
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        _pre_turn_title = conversation.title
        start_time = asyncio.get_running_loop().time()
        turn_type = "system" if system_initiated else "user"

        try:
            current_user_email.set(user_email)
            current_agent_id.set(agent.agent_id)

            if attachment_notice:
                await self._notify_observers_system_message(conversation_id, attachment_notice)

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
                        "system_initiated": system_initiated,
                    },
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

            # Handle delegation
            if decision is not None and decision.decision == "delegate":
                workflow_id = await self._select_workflow(agent, content)
                task = await self._task_queue.submit(
                    created_by=user_email,
                    agent_id=agent.agent_id,
                    title=content[:80],
                    description=content,
                    source_type="chat",
                    source_ref=conversation_id,
                    delivery=TaskDelivery(mode="same_conversation"),
                    workflow_id=workflow_id,
                    status="queued",
                )

                # Notify observers about delegation
                await self._notify_observers(
                    conversation_id,
                    "on_system_message",
                    conversation_id,
                    "Working on that in the background.",
                )

                result = TurnResult(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    delegated=True,
                    task_id=task.task_id,
                )
                await self._publish_turn_completed(result)
                TURNS_TOTAL.labels(outcome="delegated").inc()
                return

            # Build streaming callbacks from observers
            on_token, on_tool_call, on_tool_result = self._build_callbacks(
                conversation_id, session.session_id, message_id
            )

            # Execute the turn
            await self._workflow_engine.run_direct_turn(
                conversation=conversation,
                session=session,
                agent=agent,
                user_message=content,
                user_attachments=attachments,
                attachment_notice=attachment_notice,
                system_initiated=system_initiated,
                on_progress=on_token,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                cancel_event=cancel_event,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
            )

            # Post-turn housekeeping
            await self._touch_conversation(conversation_id)

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

            # Check title change
            title_changed = bool(conversation.title and conversation.title != _pre_turn_title)

            result = TurnResult(
                conversation_id=conversation_id,
                session_id=session.session_id,
                message_id=message_id,
                last_seq=last_seq,
                context_usage=context_usage,
                title_changed=title_changed,
                new_title=conversation.title if title_changed else None,
            )
            await self._publish_turn_completed(result)
            TURNS_TOTAL.labels(outcome="completed").inc()

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
            error = TurnError(
                code="turn_cancelled",
                message="The current turn was cancelled.",
                recoverable=True,
            )
            await self._publish_turn_error(conversation_id, session.session_id, error)
            TURNS_TOTAL.labels(outcome="cancelled").inc()

        except Exception as exc:
            logger.exception(
                "turn_scheduler: turn failed",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            error = await self._classify_turn_error(exc)
            await self._publish_turn_error(conversation_id, session.session_id, error)
            TURNS_TOTAL.labels(outcome="error").inc()

        finally:
            duration = asyncio.get_running_loop().time() - start_time
            TURN_DURATION.labels(type=turn_type).observe(duration)

            self._active_turns.pop(conversation_id, None)
            self._turn_controls.pop(conversation_id, None)
            self._turn_sessions.pop(conversation_id, None)
            if not system_initiated:
                count = self._user_turn_counts.get(user_email, 1)
                if count <= 1:
                    self._user_turn_counts.pop(user_email, None)
                else:
                    self._user_turn_counts[user_email] = count - 1

            # Drain queued messages
            queue = self._queued_messages.get(conversation_id)
            if queue:
                queued = queue.popleft()
                try:
                    await self.submit_turn(
                        conversation_id,
                        queued.content,
                        user_email=queued.user_email,
                        attachments=queued.attachments,
                        system_initiated=queued.system_initiated,
                    )
                except Exception:
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
    ) -> tuple[Any, Any, Any]:
        """Build streaming callbacks that fan out to registered observers."""

        async def on_token(delta: str) -> None:
            for observer in list(self._observers.get(conversation_id, [])):
                with contextlib.suppress(Exception):
                    await observer.on_token(conversation_id, session_id, message_id, delta)

        async def on_tool_call(
            tool_name: str,
            call_id: str,
            arguments: dict[str, Any] | None = None,
        ) -> None:
            for observer in list(self._observers.get(conversation_id, [])):
                with contextlib.suppress(Exception):
                    await observer.on_tool_call(
                        conversation_id, session_id, call_id, tool_name, arguments
                    )

        async def on_tool_result(
            call_id: str,
            tool_name: str,
            result: str,
            is_error: bool,
            duration_ms: int | None,
            evaluation: dict[str, Any] | None = None,
        ) -> None:
            for observer in list(self._observers.get(conversation_id, [])):
                with contextlib.suppress(Exception):
                    await observer.on_tool_result(
                        conversation_id,
                        session_id,
                        call_id,
                        tool_name,
                        result,
                        is_error,
                        duration_ms,
                        evaluation,
                    )

        return on_token, on_tool_call, on_tool_result

    async def _notify_observers(self, conversation_id: str, method: str, *args: Any) -> None:
        """Call a method on all observers for a conversation."""
        for observer in list(self._observers.get(conversation_id, [])):
            with contextlib.suppress(Exception):
                await getattr(observer, method)(*args)

    async def _notify_observers_system_message(self, conversation_id: str, text: str) -> None:
        """Send a system message to all observers."""
        await self._notify_observers(conversation_id, "on_system_message", conversation_id, text)

    async def _publish_turn_completed(self, result: TurnResult) -> None:
        """Notify observers and publish lifecycle event."""
        for observer in list(self._observers.get(result.conversation_id, [])):
            with contextlib.suppress(Exception):
                await observer.on_turn_complete(result)

        await self._event_bus.publish(
            Event(
                type=EventType.TURN_COMPLETED,
                data={
                    "conversation_id": result.conversation_id,
                    "session_id": result.session_id,
                    "message_id": result.message_id,
                    "last_seq": result.last_seq,
                    "context_usage": result.context_usage,
                    "delegated": result.delegated,
                    "task_id": result.task_id,
                    "title_changed": result.title_changed,
                    "new_title": result.new_title,
                    "queued_count": self.queued_count(result.conversation_id),
                },
            )
        )

    async def _publish_turn_error(
        self, conversation_id: str, session_id: str, error: TurnError
    ) -> None:
        """Notify observers and publish lifecycle event."""
        for observer in list(self._observers.get(conversation_id, [])):
            with contextlib.suppress(Exception):
                await observer.on_turn_error(conversation_id, error)

        await self._event_bus.publish(
            Event(
                type=EventType.TURN_ERROR,
                data={
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "error_code": error.code,
                    "error_message": error.message,
                    "recoverable": error.recoverable,
                },
            )
        )

    # ------------------------------------------------------------------
    # Workflow selection
    # ------------------------------------------------------------------

    async def _select_workflow(self, agent: Any, task_description: str) -> str:
        """Select the best workflow for a delegated task."""
        execution = agent.execution or {}
        available_ids = execution.get("available_workflow_ids")
        available_workflows = await self._workflow_registry.list_all(owner_email=agent.owner_email)
        if isinstance(available_ids, list) and available_ids:
            available_workflows = [
                w for w in available_workflows if w.workflow_id in set(available_ids)
            ]
        if not available_workflows:
            default_workflow_id = execution.get("default_workflow_id")
            return (
                default_workflow_id if isinstance(default_workflow_id, str) else "system:research"
            )
        from cognis.core.decision import select_workflow

        selection = await select_workflow(
            llm=self._providers.llm,
            task_description=task_description,
            available_workflows=[
                {
                    "workflow_id": workflow.workflow_id,
                    "name": workflow.name,
                    "criteria": workflow.criteria,
                }
                for workflow in available_workflows
            ],
            default_workflow_id=execution.get("default_workflow_id", "system:research"),
            selection_mode=execution.get("workflow_selection_mode", "automatic"),
        )
        return selection.workflow_id

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
        from cognis.store.queries import get_agent, get_conversation, get_session_row

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
                                False,
                            )

                    intention = (
                        user_message
                        or conversation_row.title
                        or f"Conversation with {agent_row.name}"
                    )
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

        if session_row is None:
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
                            False,
                        )

                compaction_summary = await self._read_compaction_summary(session_model)
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
                    )
                    ROTATION_TOTAL.labels(trigger="deferred").inc()
                except Exception as exc:
                    raise SessionCreationFailedError(
                        "Could not create session after compaction"
                    ) from exc
                conversation_model.active_session_id = new_session.session_id

                # Pre-populate session cache with compaction summary
                if compaction_summary:
                    await self._session_cache.refresh(new_session)
                    await self._session_cache.apply_compaction(
                        new_session,
                        summary=compaction_summary,
                        compaction_seq=0,
                    )

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
                return conversation_model, new_session, agent_model, False

        # Periodic cleanup of deferred creation locks (outside the lock block)
        if len(self._deferred_creation_locks) > _MAX_DEFERRED_LOCKS:
            to_remove = [
                cid for cid, lk in self._deferred_creation_locks.items() if not lk.locked()
            ]
            for cid in to_remove:
                self._deferred_creation_locks.pop(cid, None)

        return conversation_model, session_model, agent_model, False

    async def _read_compaction_summary(self, session: SessionModel) -> str | None:
        """Read the last compaction_summary event from a completed session."""
        try:
            result = await self._providers.guardrails.read_events(
                session_id=session.intaris_session_id or session.session_id,
                after_seq=0,
                allow_missing_stream=True,
            )
            for event in reversed(result.events):
                if event.get("type") == "compaction_summary":
                    return event.get("data", {}).get("summary")
        except Exception:
            logger.warning(
                "turn_scheduler: failed to read compaction summary",
                extra={"extra_data": {"session_id": session.session_id}},
            )
        return None

    async def _touch_conversation(self, conversation_id: str) -> None:
        """Update last_message_at on the conversation for unread tracking."""
        try:
            from cognis.store.queries import get_conversation

            async with self._session_factory() as db_session:
                row = await get_conversation(db_session, conversation_id)
                if row is not None:
                    row.last_message_at = datetime.now(UTC)
                    row.updated_at = row.last_message_at
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
    gate_message: str | None = None,
    gate_options: list[dict[str, Any]] | None = None,
) -> str:
    """Build a system prompt for the follow-up turn after a task/delegation completes."""
    status_name = (status or "updated").lower()

    # Task-specific prompts (from workflow engine)
    if task_id:
        title_str = f'"{task_title}"' if task_title else task_id
        if status_name == "completed":
            lines = [
                f"Background task {title_str} (task_id: {task_id}) has completed.",
            ]
            if result_summary:
                lines.append(f"\nResult summary: {result_summary}")
            lines.append(
                "\nPresent this result to the user concisely. "
                "If you need the full detailed output, use the get_task_output "
                f'tool with task_id="{task_id}".'
            )
            return "\n".join(lines)
        if status_name == "failed":
            lines = [
                f"Background task {title_str} (task_id: {task_id}) has failed.",
            ]
            if result_summary:
                lines.append(f"\nError details: {result_summary}")
            lines.append(
                "\nInform the user that the task has failed and briefly explain "
                "why based on the error details above. Do NOT attempt to complete "
                "the task yourself, do NOT call retry_task or create_task, and do "
                "NOT make additional tool calls to gather the task's results. "
                "Simply inform the user and let them decide what to do next."
            )
            return "\n".join(lines)
        if status_name == "cancelled":
            return (
                f"Background task {title_str} (task_id: {task_id}) was cancelled. "
                "Provide a brief follow-up to the user if warranted."
            )
        if status_name == "paused":
            lines = [
                f"Background task {title_str} (task_id: {task_id}) needs your attention.",
            ]
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
                "wrong. The user can retry the step (use `retry_task`), or cancel "
                "the task. Do NOT retry automatically — let the user decide."
            )
            return "\n".join(lines)
        # Generic task update
        return (
            f"Background task {title_str} (task_id: {task_id}) status: {status_name}. "
            f"Summary: {result_summary or 'No summary available.'}. "
            "Provide a concise follow-up to the user."
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
