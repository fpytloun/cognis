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
import html
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
from cognis.core.attachment_utils import (
    attachment_placeholder_text,
    normalize_attachment_refs,
    strip_attachment_payload_bytes,
)
from cognis.core.compaction import ROTATION_TOTAL
from cognis.core.errors import ImmutablePrefixUnavailable
from cognis.core.events import Event, EventBus, EventType
from cognis.core.followups import (
    FollowUpMetadata,
    parse_follow_up_metadata,
)
from cognis.core.workflow_management import (
    decode_skill_workflow_candidate_id,
    encode_skill_workflow_candidate_id,
    materialize_skill_workflow,
    skill_workflow_criteria,
)
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.session import BLOCKED_STATES, ConversationModel, SessionModel, SessionStatus
from cognis.models.task import TaskDelivery
from cognis.runtime_context import current_agent_id, current_user_email
from cognis.store.models import FollowUpDedupeRow
from cognis.tools.skills import resolve_skills_for_agent

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
FOLLOW_UP_DEDUPE_TOTAL = Counter(
    "cognis_follow_up_dedupe_total",
    "Suppressed duplicate follow-up turn requests",
    ["reason"],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_QUEUED_MESSAGES = 5
DEFAULT_TURN_LIMIT = 3
_MAX_DEFERRED_LOCKS = 200
FOLLOW_UP_DEDUPE_TTL_SECONDS = 600.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    final_content: str | None = None
    system_initiated: bool = False
    channel_deliverable: bool = False
    delivery_id: str | None = None
    delivery_fallback_text: str | None = None
    attachments: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class _QueuedMessage:
    """A message queued behind an active turn."""

    content: str
    user_email: str
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


@dataclass(slots=True)
class _TurnControl:
    """Mutable state for one active conversation turn."""

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    turn_observers: list[TurnObserver] = field(default_factory=list)
    absorbed_follow_up_ids: set[str] = field(default_factory=set)
    absorbed_outbound_attachments: list[dict[str, Any]] = field(default_factory=list)
    absorbed_channel_deliverable: bool = False
    absorbed_delivery_id: str | None = None
    absorbed_delivery_fallback_text: str | None = None


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
        self._turn_controls: dict[str, _TurnControl] = {}
        self._turn_sessions: dict[str, str] = {}
        self._queued_messages: dict[str, deque[_QueuedMessage]] = defaultdict(deque)
        self._escalation_notice_pause_ids: dict[str, str] = {}
        self._pending_follow_ups: set[tuple[str, str]] = set()
        self._handled_follow_ups: dict[tuple[str, str], float] = {}

        # Per-user concurrent turn limit
        self._user_turn_counts: dict[str, int] = defaultdict(int)

        # Conversation-scoped observers (multiple allowed — e.g. multiple browser tabs)
        self._observers: dict[str, list[TurnObserver]] = defaultdict(list)
        self._observer_failures: dict[tuple[str, int], int] = defaultdict(int)
        self._disabled_observers: set[tuple[str, int]] = set()

        # Per-conversation session creation locks (bootstrap + compaction recovery)
        self._deferred_creation_locks: dict[str, asyncio.Lock] = {}

        # Register for follow-up turn events
        event_bus.subscribe(EventType.FOLLOW_UP_TURN_REQUESTED, self._handle_follow_up_event)
        logger.info("turn_scheduler: registered on EventBus")
        logger.info("turn_scheduler: follow-up dedupe backed by durable store when available")

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

        effective_content = _effective_user_content(content, normalized_attachments)

        # Load conversation runtime only after validating attachments so
        # failed first sends do not bootstrap a session unnecessarily.
        try:
            runtime = await self._load_conversation_runtime(
                conversation_id, user_message=effective_content
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

        # Authorization check
        if not system_initiated and conversation.user_email != user_email:
            return TurnError(
                code="forbidden",
                message="Conversation access denied",
                recoverable=False,
            )

        attachment_notice, attachment_context = await self._build_attachment_support_messages(
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
                        content=effective_content,
                        user_email=user_email,
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
                    )
                )
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
                    content=effective_content,
                    user_email=user_email,
                    attachments=[item.model_dump(mode="json") for item in normalized_attachments],
                    attachment_notice=attachment_notice,
                    attachment_context=attachment_context,
                    outbound_attachments=outbound_attachments,
                    system_initiated=system_initiated,
                    follow_up=follow_up,
                    channel_deliverable=channel_deliverable,
                    delivery_id=delivery_id,
                    delivery_fallback_text=delivery_fallback_text,
                    turn_observers=tuple(turn_observers or ()),
                )
            )
            # Notify observers that the message was queued
            for observer in self._iter_observers(conversation_id, turn_observers=turn_observers):
                with contextlib.suppress(Exception):
                    await observer.on_queued(conversation_id, len(queue))
            return None

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

        # Launch the turn
        self._launch_turn(
            conversation=conversation,
            session=session,
            agent=agent,
            content=effective_content,
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
        )
        return None

    async def cancel_turn(self, conversation_id: str) -> bool:
        """Cancel the active turn and all its child sub-sessions."""
        control = self._turn_controls.get(conversation_id)
        queue = self._queued_messages.get(conversation_id)
        cleared_queue = False
        if queue is not None:
            cleared_queue = bool(queue)
            for queued in queue:
                if queued.follow_up is not None:
                    await self._clear_follow_up_pending(
                        conversation_id, queued.follow_up.follow_up_id
                    )
            queue.clear()
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

    def queued_count(self, conversation_id: str) -> int:
        """Return the number of queued messages for a conversation."""
        return len(self._queued_messages.get(conversation_id, []))

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
                        data={
                            "conversation_id": conversation_id,
                            "session_id": self._turn_sessions.get(conversation_id),
                            "content": queued.content,
                            "attachments": strip_attachment_payload_bytes(queued.attachments or []),
                        },
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
        await self._notify_observers(
            conversation_id,
            "on_queued",
            conversation_id,
            self.queued_count(conversation_id),
            turn_observers=control.turn_observers,
        )
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
        except Exception:
            logger.warning(
                "turn_scheduler: invalid follow-up metadata, dropping",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
            return
        if not await self._register_follow_up(conversation_id, follow_up.follow_up_id):
            return

        # Use submit_turn for unified serialization
        # Determine user_email from the conversation
        async with self._session_factory() as db_session:
            from cognis.store.queries import get_conversation

            row = await get_conversation(db_session, conversation_id)
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
            if attachment.kind in {ArtifactKind.FILE, ArtifactKind.VIDEO} and model_info.supports_file_input:
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
    ) -> None:
        """Launch a turn as a background asyncio.Task."""
        conversation_id = conversation.conversation_id
        control = _TurnControl(turn_observers=list(turn_observers))
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
        turn_observers: tuple[TurnObserver, ...] = (),
    ) -> None:
        """Execute a single chat turn."""
        conversation_id = conversation.conversation_id
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        _pre_turn_title = conversation.title
        start_time = asyncio.get_running_loop().time()
        turn_type = "system" if system_initiated else "user"
        turn_succeeded = False
        if turn_control is None:
            turn_control = _TurnControl(turn_observers=list(turn_observers))
        turn_observers = turn_control.turn_observers

        try:
            current_user_email.set(user_email)
            current_agent_id.set(agent.agent_id)

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
                        "system_initiated": system_initiated,
                    },
                )
            )

            # Publish USER_MESSAGE so WebSocket clients watching this
            # conversation see channel-originated messages in real time
            # (without waiting for a page refresh / history reload).
            if not system_initiated:
                published_user_content = _effective_user_content(content, attachments or [])
                await self._event_bus.publish(
                    Event(
                        type=EventType.USER_MESSAGE,
                        data={
                            "conversation_id": conversation_id,
                            "session_id": session.session_id,
                            "content": published_user_content,
                            "attachments": [
                                item.model_dump(mode="json") for item in (attachments or [])
                            ],
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
                    turn_observers=turn_observers,
                )

                result = TurnResult(
                    conversation_id=conversation_id,
                    session_id=session.session_id,
                    message_id=message_id,
                    delegated=True,
                    task_id=task.task_id,
                    system_initiated=system_initiated,
                    channel_deliverable=channel_deliverable,
                    delivery_id=delivery_id,
                    delivery_fallback_text=delivery_fallback_text,
                    attachments=normalize_attachment_refs(outbound_attachments or []),
                )
                await self._publish_turn_completed(result, turn_observers=turn_observers)
                TURNS_TOTAL.labels(outcome="delegated").inc()
                turn_succeeded = True
                return

            # Build streaming callbacks from observers
            on_token, on_tool_call, on_tool_result = self._build_callbacks(
                conversation_id, session.session_id, message_id, turn_observers=turn_observers
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
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                cancel_event=cancel_event,
                bootstrap_wait_for_intention=bootstrap_wait_for_intention,
                consume_boundary_batch=lambda reason: self._consume_queued_batch_for_active_turn(
                    conversation_id,
                    reason=reason,
                ),
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
            )
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
            error = TurnError(
                code="turn_cancelled",
                message="The current turn was cancelled.",
                recoverable=True,
            )
            await self._publish_turn_error(
                conversation_id,
                session.session_id,
                error,
                system_initiated=system_initiated,
                channel_deliverable=(
                    channel_deliverable or turn_control.absorbed_channel_deliverable
                ),
                delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                delivery_fallback_text=(
                    turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                ),
                turn_observers=turn_observers,
            )
            TURNS_TOTAL.labels(outcome="cancelled").inc()

        except Exception as exc:
            logger.exception(
                "turn_scheduler: turn failed",
                extra={"extra_data": {"conversation_id": conversation_id}},
            )
            error = await self._classify_turn_error(exc)
            await self._publish_turn_error(
                conversation_id,
                session.session_id,
                error,
                system_initiated=system_initiated,
                channel_deliverable=(
                    channel_deliverable or turn_control.absorbed_channel_deliverable
                ),
                delivery_id=turn_control.absorbed_delivery_id or delivery_id,
                delivery_fallback_text=(
                    turn_control.absorbed_delivery_fallback_text or delivery_fallback_text
                ),
                turn_observers=turn_observers,
            )
            TURNS_TOTAL.labels(outcome="error").inc()

        finally:
            duration = asyncio.get_running_loop().time() - start_time
            TURN_DURATION.labels(type=turn_type).observe(duration)

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

            self._active_turns.pop(conversation_id, None)
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

            # Drain queued messages
            queue = self._queued_messages.get(conversation_id)
            if queue:
                queued = queue.popleft()
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
        *,
        turn_observers: tuple[TurnObserver, ...] = (),
    ) -> tuple[Any, Any, Any]:
        """Build streaming callbacks that fan out to registered observers."""

        async def on_token(delta: str) -> None:
            await asyncio.gather(
                *(
                    self._call_observer(
                        conversation_id,
                        observer,
                        observer.on_token,
                        conversation_id,
                        session_id,
                        message_id,
                        delta,
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
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        async def on_tool_result(
            call_id: str,
            tool_name: str,
            result: str,
            is_error: bool,
            duration_ms: int | None,
            evaluation: dict[str, Any] | None = None,
        ) -> None:
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
                    )
                    for observer in self._iter_observers(
                        conversation_id, turn_observers=turn_observers
                    )
                )
            )

        return on_token, on_tool_call, on_tool_result

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
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Send a system message to all observers."""
        await self._notify_observers(
            conversation_id,
            "on_system_message",
            conversation_id,
            text,
            turn_observers=turn_observers,
        )

    async def _publish_turn_completed(
        self,
        result: TurnResult,
        *,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Notify observers and publish lifecycle event."""
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
                    "attachments": strip_attachment_payload_bytes(result.attachments or []),
                },
            )
        )

    async def _publish_turn_error(
        self,
        conversation_id: str,
        session_id: str,
        error: TurnError,
        *,
        system_initiated: bool = False,
        channel_deliverable: bool = False,
        delivery_id: str | None = None,
        delivery_fallback_text: str | None = None,
        turn_observers: list[TurnObserver] | tuple[TurnObserver, ...] | None = None,
    ) -> None:
        """Notify observers and publish lifecycle event."""
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

    async def _call_observer(
        self,
        conversation_id: str,
        observer: TurnObserver,
        callback: Any,
        *args: Any,
    ) -> None:
        try:
            await asyncio.wait_for(callback(*args), timeout=1.0)
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
        workflow_candidates = [
            {
                "workflow_id": workflow.workflow_id,
                "name": workflow.name,
                "criteria": workflow.criteria,
            }
            for workflow in available_workflows
        ]
        async with self._session_factory() as session:
            resolved_skills = await resolve_skills_for_agent(
                session,
                agent,
                owner_email=agent.owner_email,
            )
        for skill in resolved_skills.skills:
            if not skill.attached or not skill.steps:
                continue
            workflow_candidates.append(
                {
                    "workflow_id": encode_skill_workflow_candidate_id(skill.skill_id),
                    "name": f"Skill: {skill.name}",
                    "criteria": skill_workflow_criteria(skill),
                }
            )
        if not workflow_candidates:
            default_workflow_id = execution.get("default_workflow_id")
            if isinstance(default_workflow_id, str):
                resolved_default = await self._workflow_registry.get(
                    default_workflow_id, owner_email=agent.owner_email
                )
                if resolved_default is not None:
                    return default_workflow_id
            return "system:general-task"
        from cognis.core.decision import select_workflow

        selection = await select_workflow(
            llm=self._providers.llm,
            task_description=task_description,
            available_workflows=workflow_candidates,
            default_workflow_id=execution.get("default_workflow_id", "system:general-task"),
            selection_mode=execution.get("workflow_selection_mode", "automatic"),
        )
        selected_skill_id = decode_skill_workflow_candidate_id(selection.workflow_id)
        if selected_skill_id is None:
            return selection.workflow_id
        try:
            created_workflow = await materialize_skill_workflow(
                session_factory=self._session_factory,
                owner_email=agent.owner_email,
                skill_id=selected_skill_id,
                lifecycle="ephemeral",
                composition_source="agent_composed",
                composition_intent=task_description,
            )
        except ValueError:
            return execution.get("default_workflow_id", "system:general-task")
        return created_workflow.workflow_id

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
                                False,
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
