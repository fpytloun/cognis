"""Unified notification service for escalations, gates, and step questions.

Notifications are user-facing prompts that require resolution.  This
service owns the full lifecycle: creation, persistence, delivery via
EventBus, and resolution.  It replaces the fragmented handling that
previously existed across WebSocket handlers, REST routes, and the
workflow engine.

The database row is the synchronization authority. ``PauseWaiter`` and
the local EventBus remain low-latency fast paths, while bounded polling
allows a different controller to resolve the notification safely.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any

from prometheus_client import Counter, Histogram
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import PauseResolution, PauseWaiter, PendingPause
from cognis.core.events import Event, EventBus, EventType
from cognis.core.question_sets import normalize_questions
from cognis.core.task_execution import (
    assert_task_execution_fence,
    current_task_cancel_event,
    current_task_execution_fence,
)
from cognis.logging import get_logger
from cognis.models.session import SessionEvent
from cognis.runtime_context import scoped_runtime_context
from cognis.store.models import NotificationRow, Task
from cognis.store.queries import (
    get_agent_direct_conversation,
    get_latest_active_conversation_for_agent,
    get_managed_conversation_link_for_target,
    get_preferred_channel_account_for_agent,
    get_session_row,
    get_task,
)

logger = get_logger(__name__)

_ACTIVE_TASK_STATUSES = {"queued", "ready", "running", "paused"}
_RESOLUTION_POLL_SECONDS = 0.5
_SENSITIVE_ARGUMENT_KEY_TOKENS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "recovery",
    "secret",
    "token",
)
_DISPLAY_ARGUMENT_MAX_DEPTH = 4
_DISPLAY_ARGUMENT_MAX_ITEMS = 50
_DISPLAY_ARGUMENT_MAX_STRING_LENGTH = 4_000

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

NOTIFICATIONS_CREATED = Counter(
    "cognis_notifications_created_total",
    "Notifications created",
    ["type"],
)
NOTIFICATIONS_RESOLVED = Counter(
    "cognis_notifications_resolved_total",
    "Notifications resolved",
    ["type", "decision"],
)
NOTIFICATION_RESOLUTION_DURATION = Histogram(
    "cognis_notification_resolution_duration_seconds",
    "Time from notification creation to resolution",
    ["type"],
    buckets=[1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600],
)
NOTIFICATION_ESCALATION_SUBMIT_FAILURES = Counter(
    "cognis_notification_escalation_submit_failures_total",
    "Escalation decision submissions to Intaris that failed",
)
# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class NotificationType(StrEnum):
    ESCALATION = "escalation"
    GATE = "gate"
    STEP_QUESTION = "step_question"
    CREDENTIAL_REQUEST = "credential_request"
    AUTH_CHALLENGE = "auth_challenge"


class Notification(BaseModel):
    """In-memory representation of a notification."""

    notification_id: str
    notification_type: str
    user_email: str
    conversation_id: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    payload: dict[str, Any] = {}
    status: str = "pending"
    resolution: dict[str, Any] | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class NotificationService:
    """Unified notification lifecycle for escalations, gates, and step questions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pause_waiter: PauseWaiter,
        event_bus: EventBus,
        providers: Any,
    ) -> None:
        self._session_factory = session_factory
        self._pause_waiter = pause_waiter
        self._event_bus = event_bus
        self._providers = providers
        self.cluster_signals: Any = None

    async def _publish_cluster_change(
        self,
        *,
        conversation_id: str,
        user_email: str,
        notification_id: str,
        task_id: str | None,
        session_id: str | None,
        revision: datetime,
    ) -> None:
        if self.cluster_signals is None:
            return
        from cognis.core.cluster_signals import (
            ClusterSignalKind,
            ClusterSignalScope,
        )

        await self.cluster_signals.publish(
            ClusterSignalKind.NOTIFICATION_STATE_CHANGED,
            scope=ClusterSignalScope(
                conversation_id=conversation_id,
                session_id=session_id,
                task_id=task_id,
                notification_id=notification_id,
            ),
            revision=f"{revision.isoformat()}:{notification_id}",
        )

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    _MANAGED_LINK_HOP_CAP = 10

    async def resolve_target_conversation(
        self,
        task_id: str | None,
        conversation_id: str | None,
    ) -> str | None:
        """Resolve the user-facing conversation for a notification.

        For task-originated notifications, the target is resolved using
        the task's delivery settings — the same logic used for task result
        delivery.  This ensures escalations, gates, and step questions
        from scheduled tasks reach the user via the configured channel.

        For managed-conversation notifications (no task_id), the target
        is walked up the ``ManagedConversationLink`` chain until a
        conversation with no open link is found (i.e. the channel-bound
        parent).  This covers the delegate → managed conv → parent conv
        chain: delegate child sessions share the managed conversation's
        conversation_id, so a single link hop already covers two levels.
        Up to ``_MANAGED_LINK_HOP_CAP`` hops are followed to handle
        nested managed conversations; a visited-set prevents cycles.

        For direct-chat notifications, the conversation_id is used as-is.
        """
        if not task_id:
            return await self._resolve_managed_conversation_chain(conversation_id)

        async with self._session_factory() as db:
            task_row = await get_task(db, task_id)
            if task_row is None:
                return conversation_id

            delivery_mode = task_row.delivery_mode or "same_conversation"

            if delivery_mode == "same_conversation":
                # For chat-created tasks, source_ref is the originating conversation
                if task_row.source_type == "chat" and task_row.source_ref:
                    return task_row.source_ref
                return conversation_id

            if delivery_mode == "specific_conversation" and task_row.delivery_target:
                return task_row.delivery_target

            if delivery_mode == "latest_active_for_agent":
                latest = await get_latest_active_conversation_for_agent(
                    db, task_row.created_by, task_row.agent_id
                )
                if latest is not None:
                    return latest.conversation_id
                # Fall back to task's own conversation if no active one found
                return conversation_id

            if delivery_mode == "preferred_channel":
                account = await get_preferred_channel_account_for_agent(
                    db,
                    user_email=task_row.created_by,
                    agent_id=task_row.agent_id,
                )
                if account is not None and account.default_conversation_id:
                    return account.default_conversation_id
                direct = await get_agent_direct_conversation(
                    db,
                    task_row.created_by,
                    task_row.agent_id,
                )
                if direct is not None:
                    return direct.conversation_id
                return conversation_id

            if delivery_mode == "silent":
                # Silent tasks still need a conversation_id for the notification
                # record, but the WebSocket won't deliver it to a subscribed user.
                # Use the task's internal conversation so it's at least visible
                # in the task detail view.
                return conversation_id

        return conversation_id

    async def _resolve_managed_conversation_chain(
        self,
        conversation_id: str | None,
    ) -> str | None:
        """Walk ManagedConversationLink hops to the channel-bound parent.

        Returns the topmost controller conversation_id, or the original
        conversation_id when no link exists.  Collects managed-origin
        metadata (title, target_agent_id) from the first hop for use in
        notification payloads.
        """
        if not conversation_id:
            return conversation_id
        candidate = conversation_id
        visited: set[str] = {candidate}
        for _ in range(self._MANAGED_LINK_HOP_CAP):
            async with self._session_factory() as db:
                link = await get_managed_conversation_link_for_target(db, candidate)
            if link is None:
                break
            next_id = link.controller_conversation_id
            if next_id in visited:
                logger.warning(
                    "notification: managed conversation link cycle detected",
                    extra={"extra_data": {"conversation_id": conversation_id}},
                )
                break
            visited.add(next_id)
            candidate = next_id
        return candidate

    async def resolve_managed_origin_metadata(
        self,
        conversation_id: str,
    ) -> dict[str, Any]:
        """Return managed-origin metadata for a notification payload.

        If ``conversation_id`` is a managed target, returns a dict with
        ``managed_conversation_title`` and ``managed_target_agent_id``
        from the first link hop.  Returns an empty dict when the
        conversation is not a managed target.
        """
        async with self._session_factory() as db:
            link = await get_managed_conversation_link_for_target(db, conversation_id)
        if link is None:
            return {}
        return {
            "managed_conversation_title": link.title or "",
            "managed_target_agent_id": link.target_agent_id or "",
        }

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        notification_type: str,
        user_email: str,
        conversation_id: str,
        task_id: str | None = None,
        step_name: str | None = None,
        step_run_id: str | None = None,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
        notification_id: str | None = None,
        suppress_event: bool = False,
    ) -> Notification:
        """Create a notification, persist to DB, register PauseWaiter, publish event.

        Args:
            notification_id: Optional explicit ID.  For escalations, use
                the Intaris ``call_id`` so the existing ``/escalations/{call_id}/resolve``
                endpoint can look up the notification directly.
        """
        nid = notification_id or f"notif_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        # Resolve target conversation (task source for task-originated;
        # managed-conversation chain for non-task notifications).
        resolved_conversation_id = (
            await self.resolve_target_conversation(task_id, conversation_id)
        ) or conversation_id

        # When the notification originated inside a managed sub-conversation
        # and was redirected to the parent, enrich the payload with managed-
        # origin metadata so channel renderers can surface the context.
        enriched_payload = dict(payload or {})
        if resolved_conversation_id != conversation_id and not task_id:
            origin_meta = await self.resolve_managed_origin_metadata(conversation_id)
            if origin_meta:
                enriched_payload = {**enriched_payload, **origin_meta}

        notification = Notification(
            notification_id=nid,
            notification_type=notification_type,
            user_email=user_email,
            conversation_id=resolved_conversation_id,
            task_id=task_id,
            step_name=step_name,
            step_run_id=step_run_id,
            session_id=session_id,
            payload=enriched_payload,
            status="pending",
            created_at=now,
        )

        # Persist to DB
        async with self._session_factory() as db:
            db.add(
                NotificationRow(
                    notification_id=nid,
                    notification_type=notification_type,
                    user_email=user_email,
                    conversation_id=resolved_conversation_id,
                    task_id=task_id,
                    step_name=step_name,
                    step_run_id=step_run_id,
                    session_id=session_id,
                    payload=enriched_payload,
                    status="pending",
                    created_at=now,
                )
            )
            await db.commit()
        await self._publish_cluster_change(
            conversation_id=resolved_conversation_id,
            user_email=user_email,
            notification_id=nid,
            task_id=task_id,
            session_id=session_id,
            revision=now,
        )

        # Register PauseWaiter so the blocking coroutine can be resolved
        pause_context = (
            enriched_payload.get("context")
            if isinstance(enriched_payload.get("context"), dict)
            else None
        )
        if pause_context is None and notification_type == NotificationType.ESCALATION:
            pause_context = {
                "call_id": enriched_payload.get("call_id"),
                "tool_name": enriched_payload.get("tool_name"),
                "risk": enriched_payload.get("risk"),
                "reasoning": enriched_payload.get("reasoning"),
                "timeout_seconds": enriched_payload.get("timeout_seconds"),
            }
        self._pause_waiter.register(
            PendingPause(
                pause_id=nid,
                pause_type=notification_type,
                task_id=task_id,
                step_name=step_name,
                step_run_id=step_run_id,
                session_id=session_id,
                conversation_id=resolved_conversation_id,
                question=enriched_payload.get("message") or enriched_payload.get("question"),
                options=enriched_payload.get("options")
                if isinstance(enriched_payload.get("options"), list)
                else None,
                questions=normalize_questions(enriched_payload.get("questions"))
                if enriched_payload.get("questions") is not None
                else None,
                context=pause_context if isinstance(pause_context, dict) else None,
            )
        )

        # Publish to EventBus for real-time WebSocket delivery unless the
        # caller explicitly requested persist-only silent delivery.
        if not suppress_event:
            await self._event_bus.publish(
                Event(
                    type=EventType.NOTIFICATION_CREATED,
                    data={
                        "notification_id": nid,
                        "notification_type": notification_type,
                        "user_email": user_email,
                        "conversation_id": resolved_conversation_id,
                        "task_id": task_id,
                        "step_name": step_name,
                        "session_id": session_id,
                        "payload": enriched_payload,
                    },
                )
            )

        NOTIFICATIONS_CREATED.labels(type=notification_type).inc()
        logger.info(
            "notification: created",
            extra={
                "extra_data": {
                    "notification_id": nid,
                    "type": notification_type,
                    "conversation_id": resolved_conversation_id,
                    "task_id": task_id,
                    "managed_origin": bool(
                        enriched_payload.get("managed_conversation_title")
                        or enriched_payload.get("managed_target_agent_id")
                    ),
                }
            },
        )
        return notification

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    async def resolve(
        self,
        notification_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
        *,
        user_email: str | None = None,
        admission_guard: Callable[[Any], Awaitable[bool]] | None = None,
    ) -> bool:
        """Resolve a notification, update DB, resolve PauseWaiter.

        Returns True if the notification was resolved. Duplicate resolves for
        an already-resolved notification are treated as successful when they
        repeat the same decision and resolution payload; this keeps UI
        retries/idempotent WebSocket frames from surfacing a false "not found"
        error after an approval raced with remote Intaris reconciliation,
        without accepting conflicting duplicate input.
        """
        resolution_data = data or {}
        now = datetime.now(UTC)

        async with self._session_factory() as db:
            if admission_guard is not None and not await admission_guard(db):
                await db.rollback()
                return False
            row = await db.get(NotificationRow, notification_id)
            if row is None:
                logger.warning(
                    "notification: resolve — not found",
                    extra={"extra_data": {"notification_id": notification_id}},
                )
                return False
            if row.status != "pending":
                if _is_same_resolution(row, decision, resolution_data):
                    waiter_ok = self._pause_waiter.resolve(
                        notification_id,
                        PauseResolution(
                            decision=decision,
                            data=_resolution_waiter_data(row.resolution, resolution_data),
                        ),
                    )
                    logger.info(
                        "notification: resolve — already resolved with same decision",
                        extra={
                            "extra_data": {
                                "notification_id": notification_id,
                                "status": row.status,
                                "decision": decision,
                                "pause_waiter_ok": waiter_ok,
                            }
                        },
                    )
                    return True
                logger.info(
                    "notification: resolve — already resolved",
                    extra={
                        "extra_data": {
                            "notification_id": notification_id,
                            "status": row.status,
                        }
                    },
                )
                return False

            notification_type = row.notification_type
            created_at = row.created_at
            row_user_email = row.user_email
            conversation_id = row.conversation_id
            task_id = row.task_id
            step_name = row.step_name
            session_id = row.session_id
            notification_payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        if notification_type == NotificationType.ESCALATION:
            try:
                with scoped_runtime_context(user_email=user_email or row_user_email):
                    await self._providers.guardrails.submit_decision(
                        notification_id, decision, resolution_data.get("note")
                    )
            except Exception:
                NOTIFICATION_ESCALATION_SUBMIT_FAILURES.inc()
                logger.warning(
                    "notification: escalation decision submit failed",
                    extra={
                        "extra_data": {"notification_id": notification_id, "decision": decision}
                    },
                    exc_info=True,
                )
                return False

        async with self._session_factory() as db:
            if admission_guard is not None and not await admission_guard(db):
                await db.rollback()
                return False
            current = await db.get(NotificationRow, notification_id)
            if current is not None and current.status != "pending":
                if not _is_same_resolution(current, decision, resolution_data):
                    return False
                self._pause_waiter.resolve(
                    notification_id,
                    PauseResolution(
                        decision=decision,
                        data=_resolution_waiter_data(current.resolution, resolution_data),
                    ),
                )
                return True
            result = await db.execute(
                update(NotificationRow)
                .where(
                    NotificationRow.notification_id == notification_id,
                    NotificationRow.status == "pending",
                )
                .values(
                    status="resolved",
                    resolution={"decision": decision, "state": "resolved", **resolution_data},
                    resolved_at=now,
                )
            )
            await db.commit()
            if not int(getattr(result, "rowcount", 0) or 0):
                current = await db.get(NotificationRow, notification_id)
                if current is None or not _is_same_resolution(current, decision, resolution_data):
                    return False

        # Local synchronization is only a fast path. The DB transition above
        # is authoritative and succeeds even when another controller owns the waiter.
        ok = self._pause_waiter.resolve(
            notification_id,
            PauseResolution(decision=decision, data=resolution_data),
        )

        await self._record_user_interaction(
            notification_id=notification_id,
            notification_type=str(notification_type),
            notification_payload=notification_payload,
            resolution_data=resolution_data,
            decision=decision,
            session_id=session_id,
            user_email=row_user_email,
        )

        # Publish resolution event
        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_RESOLVED,
                data={
                    "notification_id": notification_id,
                    "notification_type": notification_type,
                    "user_email": row_user_email,
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "step_name": step_name,
                    "session_id": session_id,
                    "decision": decision,
                },
            )
        )
        await self._publish_cluster_change(
            conversation_id=conversation_id,
            user_email=row_user_email,
            notification_id=notification_id,
            task_id=task_id,
            session_id=session_id,
            revision=now,
        )

        # Metrics — normalize decision to a known set to prevent cardinality explosion
        _KNOWN_DECISIONS = {"approve", "deny", "continue", "cancel"}
        safe_decision = decision if decision in _KNOWN_DECISIONS else "other"
        NOTIFICATIONS_RESOLVED.labels(type=notification_type, decision=safe_decision).inc()
        if created_at:
            created_dt = (
                created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
            )
            duration = (now - created_dt).total_seconds()
            NOTIFICATION_RESOLUTION_DURATION.labels(type=notification_type).observe(duration)

        logger.info(
            "notification: resolved",
            extra={
                "extra_data": {
                    "notification_id": notification_id,
                    "type": notification_type,
                    "decision": decision,
                    "pause_waiter_ok": ok,
                }
            },
        )
        return True

    async def wait_for_resolution(
        self,
        notification_id: str,
        *,
        timeout: float,
        cancel_event: asyncio.Event | None = None,
        poll_seconds: float = _RESOLUTION_POLL_SECONDS,
    ) -> PauseResolution:
        """Wait on the local fast path while polling the authoritative DB row."""
        cancel_event = cancel_event or current_task_cancel_event()
        execution_fence = current_task_execution_fence()
        if execution_fence is not None:
            await execution_fence.suspend_capacity()
        deadline = monotonic() + timeout if timeout > 0 else None
        local_wait = asyncio.create_task(self._pause_waiter.wait(notification_id, timeout=timeout))
        resolved: PauseResolution | None = None
        try:
            while True:
                if local_wait.done() and resolved is None:
                    resolved = await local_wait
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError
                if resolved is None and deadline is not None and monotonic() >= deadline:
                    raise TimeoutError

                async with self._session_factory() as db:
                    await assert_task_execution_fence(db)
                    if execution_fence is not None:
                        task = await db.get(Task, execution_fence.claim.task_id)
                        if task is not None and task.status == "cancelled":
                            if cancel_event is not None:
                                cancel_event.set()
                            raise asyncio.CancelledError
                    if resolved is None:
                        row = await db.get(NotificationRow, notification_id)
                        if row is None:
                            raise LookupError(f"Notification {notification_id} not found")
                        if row.status == "resolved" and isinstance(row.resolution, dict):
                            decision = str(row.resolution.get("decision") or "deny")
                            resolved = PauseResolution(
                                decision=decision,
                                data=_resolution_waiter_data(row.resolution, {}),
                            )

                if resolved is not None and (
                    execution_fence is None or await execution_fence.ensure_capacity()
                ):
                    return resolved

                delay = poll_seconds
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - monotonic()))
                if cancel_event is None:
                    await asyncio.sleep(delay)
                else:
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    else:
                        raise asyncio.CancelledError
        finally:
            if not local_wait.done():
                local_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await local_wait

    async def _record_user_interaction(
        self,
        *,
        notification_id: str,
        notification_type: str,
        notification_payload: dict[str, Any],
        resolution_data: dict[str, Any],
        decision: str,
        session_id: str | None,
        user_email: str,
    ) -> None:
        """Persist a safe display projection of a resolved user interaction.

        This deliberately records no raw credential, OTP, or provider response
        payload. The durable event is a timeline record, not a replayable input
        envelope for the paused tool.
        """

        if not session_id:
            return
        try:
            async with self._session_factory() as db:
                session_row = await get_session_row(db, session_id)
            intaris_session_id = (
                session_row.intaris_session_id if session_row is not None else session_id
            ) or session_id
        except Exception:
            logger.warning(
                "notification: failed to resolve Intaris session for interaction timeline event",
                extra={
                    "extra_data": {"notification_id": notification_id, "session_id": session_id}
                },
                exc_info=True,
            )
            intaris_session_id = session_id
        event = SessionEvent(
            type="lifecycle",
            data={
                "event": "user_interaction_resolved",
                "interaction_id": notification_id,
                "interaction_type": notification_type,
                "origin_call_id": notification_payload.get("origin_call_id"),
                "origin_tool_name": notification_payload.get("origin_tool_name"),
                **_user_interaction_display(
                    notification_type=notification_type,
                    notification_payload=notification_payload,
                    resolution_data=resolution_data,
                    decision=decision,
                ),
            },
        )
        try:
            with scoped_runtime_context(user_email=user_email):
                result = await self._providers.guardrails.record_events(
                    session_id=intaris_session_id,
                    events=[event],
                    source="cognis",
                    idempotency_key=f"{intaris_session_id}:user_interaction:{notification_id}",
                    user_email=user_email,
                )
            if not result.ok:
                raise RuntimeError("Intaris did not persist user interaction")
        except Exception:
            logger.warning(
                "notification: failed to persist user interaction timeline event",
                extra={
                    "extra_data": {
                        "notification_id": notification_id,
                        "notification_type": notification_type,
                        "session_id": intaris_session_id,
                    }
                },
                exc_info=True,
            )

    async def resolve_internal(
        self,
        notification_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
        *,
        admission_guard: Callable[[Any], Awaitable[bool]] | None = None,
    ) -> bool:
        """Resolve callback-only notifications from trusted internal services."""

        return await self.resolve(
            notification_id,
            decision,
            data,
            user_email=None,
            admission_guard=admission_guard,
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def list_pending(
        self,
        user_email: str,
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> list[Notification]:
        """List pending notifications for a user, optionally filtered by conversation."""
        stale_notifications: list[tuple[str, str]] = []
        resolved_submitted: set[str] = set()
        async with self._session_factory() as db:
            stmt = select(NotificationRow).where(
                NotificationRow.user_email == user_email,
                NotificationRow.status == "pending",
            )
            if conversation_id:
                stmt = stmt.where(NotificationRow.conversation_id == conversation_id)
            if task_id:
                stmt = stmt.where(NotificationRow.task_id == task_id)
            if session_id:
                stmt = stmt.where(NotificationRow.session_id == session_id)
            stmt = stmt.order_by(NotificationRow.created_at.desc())
            result = await db.execute(stmt)
            rows = result.scalars().all()

        for row in rows:
            if await self._reconcile_remote_escalation(row):
                resolved_submitted.add(row.notification_id)

        async with self._session_factory() as db:
            visible_rows: list[Notification] = []
            task_status_cache: dict[str, str | None] = {}
            now = datetime.now(UTC)
            for row in rows:
                if row.notification_id in resolved_submitted:
                    continue
                if _is_expired_escalation(row, now=now):
                    stale_notifications.append((row.notification_id, "timeout"))
                    continue
                if row.task_id:
                    status = task_status_cache.get(row.task_id)
                    if row.task_id not in task_status_cache:
                        task_row = await get_task(db, row.task_id)
                        status = str(task_row.status) if task_row is not None else None
                        task_status_cache[row.task_id] = status
                    if status not in _ACTIVE_TASK_STATUSES:
                        stale_notifications.append((row.notification_id, "task_terminal"))
                        continue
                visible_rows.append(_row_to_notification(row))

        for notification_id, reason in stale_notifications:
            await self.mark_orphaned(notification_id, reason=reason)

        return visible_rows

    async def _reconcile_remote_escalation(self, row: NotificationRow) -> bool:
        """Resolve locally when Intaris already recorded an external decision."""
        if row.notification_type != NotificationType.ESCALATION or row.status != "pending":
            return False
        resolution = row.resolution if isinstance(row.resolution, dict) else {}

        try:
            with scoped_runtime_context(user_email=row.user_email):
                remote = await self._providers.guardrails.get_escalation(row.notification_id)
        except Exception:
            logger.warning(
                "notification: failed to reconcile external escalation decision",
                extra={"extra_data": {"notification_id": row.notification_id}},
                exc_info=True,
            )
            return False
        if remote is None:
            return False
        remote_user_decision = str(getattr(remote, "user_decision", "") or "").strip().lower()
        remote_resolved = bool(remote.resolved or remote_user_decision)
        if not remote_resolved:
            return False

        if remote_user_decision in {"approve", "deny"}:
            decision = remote_user_decision
        else:
            decision = remote.decision or str(resolution.get("decision") or "approve")
        remote_note = getattr(remote, "user_note", None)
        note = str(remote_note if remote_note is not None else resolution.get("note", ""))
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            result = await db.execute(
                update(NotificationRow)
                .where(
                    NotificationRow.notification_id == row.notification_id,
                    NotificationRow.status == "pending",
                )
                .values(
                    status="resolved",
                    resolution={
                        **resolution,
                        "decision": decision,
                        "note": note,
                        "state": "resolved_remote",
                    },
                    resolved_at=now,
                )
            )
            await db.commit()
            if not int(getattr(result, "rowcount", 0) or 0):
                current = await db.get(NotificationRow, row.notification_id)
                if current is None or not _is_same_resolution(current, decision, {"note": note}):
                    return False

        self._pause_waiter.resolve(
            row.notification_id,
            PauseResolution(decision=decision, data={"note": note}),
        )

        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_RESOLVED,
                data={
                    "notification_id": row.notification_id,
                    "notification_type": row.notification_type,
                    "user_email": row.user_email,
                    "conversation_id": row.conversation_id,
                    "task_id": row.task_id,
                    "step_name": row.step_name,
                    "session_id": row.session_id,
                    "decision": decision,
                },
            )
        )
        await self._publish_cluster_change(
            conversation_id=row.conversation_id,
            user_email=row.user_email,
            notification_id=row.notification_id,
            task_id=row.task_id,
            session_id=row.session_id,
            revision=now,
        )

        safe_decision = (
            decision if decision in {"approve", "deny", "continue", "cancel"} else "other"
        )
        NOTIFICATIONS_RESOLVED.labels(type=row.notification_type, decision=safe_decision).inc()
        if row.created_at:
            created_at = (
                row.created_at
                if row.created_at.tzinfo is not None
                else row.created_at.replace(tzinfo=UTC)
            )
            NOTIFICATION_RESOLUTION_DURATION.labels(type=row.notification_type).observe(
                (now - created_at).total_seconds()
            )

        logger.info(
            "notification: reconciled external escalation decision",
            extra={
                "extra_data": {
                    "notification_id": row.notification_id,
                    "decision": decision,
                    "task_id": row.task_id,
                }
            },
        )
        return True

    async def reconcile_remote_escalation(self, notification_id: str) -> bool:
        """Resolve a pending escalation if Intaris recorded an external decision."""
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, notification_id)
            if row is None:
                return False
        return await self._reconcile_remote_escalation(row)

    async def get(self, notification_id: str) -> Notification | None:
        """Get a single notification by ID."""
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, notification_id)
            if row is None:
                return None
            return _row_to_notification(row)

    async def find_by_task(
        self,
        task_id: str,
        *,
        notification_type: str | None = None,
        status: str = "pending",
    ) -> Notification | None:
        """Find a notification by task_id (and optionally type/status)."""
        async with self._session_factory() as db:
            stmt = select(NotificationRow).where(
                NotificationRow.task_id == task_id,
                NotificationRow.status == status,
            )
            if notification_type:
                stmt = stmt.where(NotificationRow.notification_type == notification_type)
            stmt = stmt.order_by(NotificationRow.created_at.desc()).limit(1)
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_notification(row)

    async def mark_orphaned(self, notification_id: str, *, reason: str) -> bool:
        """Mark a pending notification as terminal without resuming a waiter."""
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, notification_id)
            if row is None or row.status != "pending":
                return False
            await db.execute(
                update(NotificationRow)
                .where(NotificationRow.notification_id == notification_id)
                .values(
                    status="resolved",
                    resolution={"decision": "cancel", "reason": reason},
                    resolved_at=now,
                )
            )
            await db.commit()
        await self._publish_cluster_change(
            conversation_id=row.conversation_id,
            user_email=row.user_email,
            notification_id=row.notification_id,
            task_id=row.task_id,
            session_id=row.session_id,
            revision=now,
        )
        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_RESOLVED,
                data={
                    "notification_id": row.notification_id,
                    "notification_type": row.notification_type,
                    "user_email": row.user_email,
                    "conversation_id": row.conversation_id,
                    "task_id": row.task_id,
                    "step_name": row.step_name,
                    "session_id": row.session_id,
                    "decision": "cancel",
                },
            )
        )
        NOTIFICATIONS_RESOLVED.labels(type=row.notification_type, decision="cancel").inc()
        if row.created_at:
            created_at = row.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            duration = (now - created_at).total_seconds()
            NOTIFICATION_RESOLUTION_DURATION.labels(type=row.notification_type).observe(duration)
        logger.info(
            "notification: orphaned",
            extra={
                "extra_data": {
                    "notification_id": notification_id,
                    "type": row.notification_type,
                    "reason": reason,
                }
            },
        )
        return True

    async def mark_task_notifications_terminal(self, task_id: str, *, reason: str) -> int:
        """Resolve any pending notifications still attached to a terminal task."""

        async with self._session_factory() as db:
            result = await db.execute(
                select(NotificationRow.notification_id).where(
                    NotificationRow.task_id == task_id,
                    NotificationRow.status == "pending",
                )
            )
            notification_ids = list(result.scalars().all())

        resolved = 0
        for notification_id in notification_ids:
            if await self.mark_orphaned(notification_id, reason=reason):
                resolved += 1
        return resolved

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def reconcile_pending(self) -> int:
        """Re-register PauseWaiters for pending notifications after restart.

        Called during startup to ensure that notifications created before
        a restart can still be resolved via REST or WebSocket.  The
        workflow engine / agent loop coroutines that were waiting are
        gone, but the task queue's resume mechanism will re-trigger them
        when the notification is resolved.

        Returns the number of notifications reconciled.
        """
        async with self._session_factory() as db:
            stmt = select(NotificationRow).where(NotificationRow.status == "pending")
            result = await db.execute(stmt)
            rows = result.scalars().all()
            task_status_cache: dict[str, str | None] = {}
            for row in rows:
                if row.task_id and row.task_id not in task_status_cache:
                    task_row = await get_task(db, row.task_id)
                    task_status_cache[row.task_id] = (
                        str(task_row.status) if task_row is not None else None
                    )

        count = 0
        orphaned_count = 0
        for row in rows:
            if row.notification_type == NotificationType.STEP_QUESTION and row.task_id is None:
                await self.mark_orphaned(
                    row.notification_id,
                    reason="controller_restart",
                )
                orphaned_count += 1
                continue
            if _is_expired_escalation(row, now=datetime.now(UTC)):
                await self.mark_orphaned(
                    row.notification_id,
                    reason="timeout",
                )
                orphaned_count += 1
                continue
            if row.task_id:
                status = task_status_cache.get(row.task_id)
                if status not in _ACTIVE_TASK_STATUSES:
                    await self.mark_orphaned(
                        row.notification_id,
                        reason="task_terminal",
                    )
                    orphaned_count += 1
                    continue
            # Only re-register if not already present (idempotent)
            existing = self._pause_waiter.get(row.notification_id)
            if existing is not None:
                continue
            self._pause_waiter.register(
                PendingPause(
                    pause_id=row.notification_id,
                    pause_type=row.notification_type,
                    task_id=row.task_id,
                    step_name=row.step_name,
                    step_run_id=row.step_run_id,
                    session_id=row.session_id,
                    conversation_id=row.conversation_id,
                    question=(row.payload or {}).get("message")
                    or (row.payload or {}).get("question"),
                    options=(row.payload or {}).get("options")
                    if isinstance((row.payload or {}).get("options"), list)
                    else None,
                    questions=normalize_questions((row.payload or {}).get("questions"))
                    if (row.payload or {}).get("questions") is not None
                    else None,
                    context=(row.payload or {}).get("context")
                    if isinstance((row.payload or {}).get("context"), dict)
                    else None,
                )
            )
            count += 1

        if count:
            logger.info(
                "notification: reconciled %d pending notifications after restart",
                count,
            )
        if orphaned_count:
            logger.info(
                "notification: marked %d direct-chat step questions orphaned after restart",
                orphaned_count,
            )
        return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_notification(row: NotificationRow) -> Notification:
    return Notification(
        notification_id=row.notification_id,
        notification_type=row.notification_type,
        user_email=row.user_email,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        step_name=row.step_name,
        step_run_id=row.step_run_id,
        session_id=row.session_id,
        payload=row.payload or {},
        status=row.status,
        resolution=row.resolution,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


def safe_display_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return bounded, recursively redacted arguments safe for user display."""

    return {
        str(key): _safe_display_argument_value(value, key=str(key), depth=0)
        for key, value in list(arguments.items())[:_DISPLAY_ARGUMENT_MAX_ITEMS]
    }


def _safe_display_argument_value(value: Any, *, key: str, depth: int) -> Any:
    if _is_sensitive_argument_key(key):
        return "[redacted]"
    if depth >= _DISPLAY_ARGUMENT_MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, str):
        return value[:_DISPLAY_ARGUMENT_MAX_STRING_LENGTH] + (
            "…" if len(value) > _DISPLAY_ARGUMENT_MAX_STRING_LENGTH else ""
        )
    if isinstance(value, dict):
        return {
            str(child_key): _safe_display_argument_value(
                child_value, key=str(child_key), depth=depth + 1
            )
            for child_key, child_value in list(value.items())[:_DISPLAY_ARGUMENT_MAX_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_display_argument_value(child, key=key, depth=depth + 1)
            for child in value[:_DISPLAY_ARGUMENT_MAX_ITEMS]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_DISPLAY_ARGUMENT_MAX_STRING_LENGTH]


def _is_sensitive_argument_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(token in normalized for token in _SENSITIVE_ARGUMENT_KEY_TOKENS)


def _user_interaction_display(
    *,
    notification_type: str,
    notification_payload: dict[str, Any],
    resolution_data: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    """Build the allowlisted display payload for a resolved user interaction."""

    status = _interaction_status(decision)
    if notification_type == NotificationType.STEP_QUESTION:
        return {
            "title": "You answered questions"
            if status == "complete"
            else "You cancelled questions",
            "summary": None,
            "answers": _question_answers_for_display(
                notification_payload.get("questions"),
                resolution_data,
            ),
            "status": status,
        }
    if notification_type == NotificationType.CREDENTIAL_REQUEST:
        credential_id = str(
            resolution_data.get("credential_id")
            or notification_payload.get("credential_id")
            or "credential"
        )
        credential_kind = str(
            resolution_data.get("credential_kind")
            or notification_payload.get("kind")
            or "credential"
        )
        if status == "complete":
            return {
                "title": "You provided a credential",
                "summary": f"Created or updated credential `{credential_id}` ({credential_kind}).",
                "answers": [],
                "status": status,
            }
        return {
            "title": f"You {status} a credential request",
            "summary": f"Credential `{credential_id}` was not provided.",
            "answers": [],
            "status": status,
        }
    if notification_type == NotificationType.AUTH_CHALLENGE:
        label = str(notification_payload.get("label") or "authentication")
        return {
            "title": (
                "You completed authentication"
                if status == "complete"
                else f"You {status} an authentication challenge"
            ),
            "summary": label,
            "answers": [],
            "status": status,
        }
    if notification_type == NotificationType.GATE:
        return _decision_interaction_display(
            decision=decision,
            noun="workflow gate",
            feedback=resolution_data.get("feedback"),
            context=None,
        )
    if notification_type == NotificationType.ESCALATION:
        return _decision_interaction_display(
            decision=decision,
            noun="action",
            feedback=resolution_data.get("note"),
            context=_escalation_context_for_display(notification_payload),
        )
    return _decision_interaction_display(
        decision=decision,
        noun=notification_type.replace("_", " "),
        feedback=None,
        context=None,
    )


def _interaction_status(decision: str) -> str:
    normalized = decision.strip().lower()
    if normalized in {"cancel", "cancelled"}:
        return "cancelled"
    if normalized in {"deny", "denied"}:
        return "denied"
    if normalized in {"fail", "failed"}:
        return "failed"
    return "complete"


def _question_answers_for_display(
    questions: Any, resolution_data: dict[str, Any]
) -> list[dict[str, str]]:
    if not isinstance(questions, list):
        return []
    answer_by_id: dict[str, dict[str, Any]] = {}
    for raw_answer in resolution_data.get("answers") or []:
        if isinstance(raw_answer, dict) and isinstance(raw_answer.get("question_id"), str):
            answer_by_id[raw_answer["question_id"]] = raw_answer

    display_answers: list[dict[str, str]] = []
    for raw_question in questions:
        if not isinstance(raw_question, dict):
            continue
        question_id = raw_question.get("id")
        question = raw_question.get("question")
        if not isinstance(question_id, str) or not isinstance(question, str):
            continue
        answer = answer_by_id.get(question_id)
        if answer is None:
            continue
        option_labels = {
            str(option.get("id")): str(option.get("label"))
            for option in raw_question.get("options") or []
            if isinstance(option, dict)
            and option.get("id") is not None
            and option.get("label") is not None
        }
        selected = [
            option_labels[option_id]
            for option_id in answer.get("selected_option_ids") or []
            if isinstance(option_id, str) and option_id in option_labels
        ]
        custom = answer.get("custom_answer")
        if isinstance(custom, str) and custom.strip():
            selected.append(custom.strip())
        if selected:
            display_answers.append({"question": question, "answer": ", ".join(selected)})
    return display_answers


def _decision_interaction_display(
    *,
    decision: str,
    noun: str,
    feedback: Any,
    context: list[dict[str, str]] | None,
) -> dict[str, Any]:
    action = {
        "approve": "approved",
        "deny": "denied",
        "continue": "continued",
        "cancel": "cancelled",
    }.get(decision.strip().lower(), decision.strip().lower() or "completed")
    title = f"You {action} the {noun}"
    answers = list(context or [])
    if isinstance(feedback, str) and feedback.strip():
        answers.append({"question": "Feedback", "answer": feedback.strip()})
    return {
        "title": title[0].upper() + title[1:],
        "summary": None,
        "answers": answers,
        "status": _interaction_status(decision),
    }


def _escalation_context_for_display(payload: dict[str, Any]) -> list[dict[str, str]]:
    answers: list[dict[str, str]] = []
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        answers.append({"question": "Action", "answer": tool_name})
    arguments = payload.get("arguments_display")
    if isinstance(arguments, dict) and arguments:
        import json

        answers.append(
            {
                "question": "Arguments",
                "answer": json.dumps(arguments, ensure_ascii=False, indent=2, sort_keys=True),
            }
        )
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        answers.append({"question": "Reason", "answer": reasoning})
    risk = payload.get("risk")
    if isinstance(risk, str) and risk:
        answers.append({"question": "Risk", "answer": risk})
    return answers


def _is_same_resolution(
    row: NotificationRow, decision: str, resolution_data: dict[str, Any]
) -> bool:
    """Return true when a terminal notification already has this resolution."""
    if row.status != "resolved" or not isinstance(row.resolution, dict):
        return False
    if str(row.resolution.get("decision") or "").lower() != decision.lower():
        return False
    if row.resolution.get("state") == "resolved_remote":
        return True
    persisted_data = {
        key: value for key, value in row.resolution.items() if key not in {"decision", "state"}
    }
    return _normalize_resolution_data(persisted_data) == _normalize_resolution_data(resolution_data)


def _normalize_resolution_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize semantically equivalent persisted/input resolution payloads."""
    normalized = dict(data)
    if normalized.get("note") == "":
        normalized.pop("note")
    return normalized


def _resolution_waiter_data(
    resolution: dict[str, Any] | None,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Return PauseResolution data from persisted resolution plus current input."""
    data = {
        key: value for key, value in (resolution or {}).items() if key not in {"decision", "state"}
    }
    data.update(fallback)
    return data


def _is_expired_escalation(row: NotificationRow, *, now: datetime) -> bool:
    """Return true when an escalation prompt has exceeded its approval window."""
    if row.notification_type != NotificationType.ESCALATION or row.status != "pending":
        return False
    created_at = row.created_at
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    timeout_raw = (
        (row.payload or {}).get("timeout_seconds") if isinstance(row.payload, dict) else None
    )
    try:
        timeout_seconds = float(timeout_raw)
    except (TypeError, ValueError):
        timeout_seconds = 300.0
    if timeout_seconds <= 0:
        return True
    return now >= created_at + timedelta(seconds=timeout_seconds)
