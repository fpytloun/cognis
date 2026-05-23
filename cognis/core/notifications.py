"""Unified notification service for escalations, gates, and step questions.

Notifications are user-facing prompts that require resolution.  This
service owns the full lifecycle: creation, persistence, delivery via
EventBus, and resolution.  It replaces the fragmented handling that
previously existed across WebSocket handlers, REST routes, and the
workflow engine.

The in-memory ``PauseWaiter`` remains the synchronization primitive
for blocking agent-loop / workflow-engine coroutines.  This service
adds DB persistence on top so notifications survive restarts and are
queryable via a unified REST endpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from prometheus_client import Counter, Histogram
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.agent_loop import PauseResolution, PauseWaiter, PendingPause
from cognis.core.events import Event, EventBus, EventType
from cognis.logging import get_logger
from cognis.runtime_context import scoped_runtime_context
from cognis.store.models import NotificationRow
from cognis.store.queries import get_latest_active_conversation_for_agent, get_task

logger = get_logger(__name__)

_ACTIVE_TASK_STATUSES = {"queued", "ready", "running", "paused"}

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
NOTIFICATION_ESCALATION_RECOVERY_PENDING = Counter(
    "cognis_notification_escalation_recovery_pending_total",
    "Escalations left pending after Intaris approval was submitted but local resume could not proceed",
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

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

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

        For direct-chat notifications, the conversation_id is used as-is.
        """
        if not task_id:
            return conversation_id

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

            if delivery_mode in ("latest_active_for_agent", "preferred_channel"):
                latest = await get_latest_active_conversation_for_agent(
                    db, task_row.created_by, task_row.agent_id
                )
                if latest is not None:
                    return latest.conversation_id
                # Fall back to task's own conversation if no active one found
                return conversation_id

            if delivery_mode == "silent":
                # Silent tasks still need a conversation_id for the notification
                # record, but the WebSocket won't deliver it to a subscribed user.
                # Use the task's internal conversation so it's at least visible
                # in the task detail view.
                return conversation_id

        return conversation_id

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
    ) -> Notification:
        """Create a notification, persist to DB, register PauseWaiter, publish event.

        Args:
            notification_id: Optional explicit ID.  For escalations, use
                the Intaris ``call_id`` so the existing ``/escalations/{call_id}/resolve``
                endpoint can look up the notification directly.
        """
        nid = notification_id or f"notif_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)

        # Resolve target conversation (task source for task-originated)
        resolved_conversation_id = (
            await self.resolve_target_conversation(task_id, conversation_id)
        ) or conversation_id

        notification = Notification(
            notification_id=nid,
            notification_type=notification_type,
            user_email=user_email,
            conversation_id=resolved_conversation_id,
            task_id=task_id,
            step_name=step_name,
            step_run_id=step_run_id,
            session_id=session_id,
            payload=payload or {},
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
                    payload=payload or {},
                    status="pending",
                    created_at=now,
                )
            )
            await db.commit()

        # Register PauseWaiter so the blocking coroutine can be resolved
        pause_context = (
            (payload or {}).get("context")
            if isinstance((payload or {}).get("context"), dict)
            else None
        )
        if pause_context is None and notification_type == NotificationType.ESCALATION:
            pause_context = {
                "call_id": (payload or {}).get("call_id"),
                "tool_name": (payload or {}).get("tool_name"),
                "risk": (payload or {}).get("risk"),
                "reasoning": (payload or {}).get("reasoning"),
                "timeout_seconds": (payload or {}).get("timeout_seconds"),
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
                question=str((payload or {}).get("question", "")),
                options=(payload or {}).get("options"),
                context=pause_context if isinstance(pause_context, dict) else None,
            )
        )

        # Publish to EventBus for real-time WebSocket delivery
        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_CREATED,
                data={
                    "notification_id": nid,
                    "notification_type": notification_type,
                    "conversation_id": resolved_conversation_id,
                    "task_id": task_id,
                    "step_name": step_name,
                    "session_id": session_id,
                    "payload": payload or {},
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
    ) -> bool:
        """Resolve a notification, update DB, resolve PauseWaiter.

        Returns True if the notification was resolved, False if it was
        already resolved or not found.
        """
        resolution_data = data or {}
        now = datetime.now(UTC)

        async with self._session_factory() as db:
            row = await db.get(NotificationRow, notification_id)
            if row is None:
                logger.warning(
                    "notification: resolve — not found",
                    extra={"extra_data": {"notification_id": notification_id}},
                )
                return False
            if row.status != "pending":
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

        # Resolve PauseWaiter (unblocks the agent loop / workflow engine)
        ok = self._pause_waiter.resolve(
            notification_id,
            PauseResolution(decision=decision, data=resolution_data),
        )

        async with self._session_factory() as db:
            if (
                notification_type == NotificationType.ESCALATION
                and not ok
                and decision == "approve"
            ):
                await db.execute(
                    update(NotificationRow)
                    .where(NotificationRow.notification_id == notification_id)
                    .values(
                        resolution={"decision": decision, "state": "submitted", **resolution_data},
                    )
                )
                await db.commit()
                NOTIFICATION_ESCALATION_RECOVERY_PENDING.inc()
                logger.warning(
                    "notification: escalation submitted to Intaris but local resume is pending recovery",
                    extra={
                        "extra_data": {"notification_id": notification_id, "decision": decision}
                    },
                )
                return False

            await db.execute(
                update(NotificationRow)
                .where(NotificationRow.notification_id == notification_id)
                .values(
                    status="resolved",
                    resolution={"decision": decision, "state": "resolved", **resolution_data},
                    resolved_at=now,
                )
            )
            await db.commit()

        # Publish resolution event
        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_RESOLVED,
                data={
                    "notification_id": notification_id,
                    "notification_type": notification_type,
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "step_name": step_name,
                    "session_id": session_id,
                    "decision": decision,
                },
            )
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
        pause_waiter_ok = self._pause_waiter.resolve(
            row.notification_id,
            PauseResolution(decision=decision, data={"note": note}),
        )

        if not pause_waiter_ok:
            async with self._session_factory() as db:
                await db.execute(
                    update(NotificationRow)
                    .where(NotificationRow.notification_id == row.notification_id)
                    .values(
                        resolution={
                            **resolution,
                            "decision": decision,
                            "note": note,
                            "state": "submitted_remote",
                        },
                    )
                )
                await db.commit()
            NOTIFICATION_ESCALATION_RECOVERY_PENDING.inc()
            logger.warning(
                "notification: external escalation decision recorded but local resume is pending recovery",
                extra={
                    "extra_data": {"notification_id": row.notification_id, "decision": decision}
                },
            )
            return False

        now = datetime.now(UTC)
        async with self._session_factory() as db:
            await db.execute(
                update(NotificationRow)
                .where(NotificationRow.notification_id == row.notification_id)
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

        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_RESOLVED,
                data={
                    "notification_id": row.notification_id,
                    "notification_type": row.notification_type,
                    "decision": decision,
                },
            )
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
                    question=str((row.payload or {}).get("question", "")),
                    options=(row.payload or {}).get("options"),
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
