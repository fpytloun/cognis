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
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any

from prometheus_client import Counter, Histogram
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from cognis.core.agent_loop import PauseResolution, PauseWaiter, PendingPause
from cognis.core.events import Event, EventBus, EventType
from cognis.core.question_sets import normalize_questions, validate_reply_for_questions
from cognis.core.questions import (
    AGENT_QUESTION,
    QuestionOrigin,
    QuestionRouting,
    QuestionTaskResumer,
)
from cognis.core.task_execution import (
    assert_task_execution_fence,
    current_task_cancel_event,
    current_task_execution_fence,
)
from cognis.logging import get_logger
from cognis.models.session import SessionEvent
from cognis.models.workflow import effective_gate_options
from cognis.providers.guardrails.resolution_errors import ResolutionRejected
from cognis.runtime_context import scoped_runtime_context
from cognis.store.models import DirectTurnRequestRow, NotificationRow, Schedule, Task
from cognis.store.queries import (
    get_agent_direct_conversation,
    get_conversation,
    get_latest_active_conversation_for_agent,
    get_managed_conversation_link_for_target,
    get_preferred_channel_account_for_agent,
    get_session_row,
    get_step_run,
    get_task,
)

logger = get_logger(__name__)

_ACTIVE_TASK_STATUSES = {"queued", "ready", "running", "paused"}
_RESOLUTION_POLL_SECONDS = 0.5
_WAIT_RECONCILE_SECONDS = 5.0
_PAUSE_RECONCILE_TIMEOUT_SECONDS = 5.0
NOTIFICATION_WAIT_READS = Counter(
    "cognis_notification_wait_reads_total",
    "Authoritative notification wait reconciliation passes.",
)
_RESOLUTION_CLAIM_SECONDS = 30.0
_RESOLUTION_COMPLETION_GRACE_SECONDS = 2.0
_INTARIS_SUBMISSION_TIMEOUT_SECONDS = 25.0
_INTERACTION_RECORD_TIMEOUT_SECONDS = 2.0
_CLUSTER_PUBLISH_TIMEOUT_SECONDS = 2.0
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


def _safe_schedule_error_summary(value: object) -> str:
    text = str(value or "Scheduled run failed").splitlines()[0].strip()
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "api_key",
            "api key",
            "password",
            "secret",
            "token",
            "authorization",
            "bearer ",
        )
    ) or ("://" in lowered and "@" in lowered):
        return "Scheduled run failed"
    return text[:240] or "Scheduled run failed"


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
NOTIFICATION_CREATE_STAGE_DURATION = Histogram(
    "cognis_notification_create_stage_duration_seconds",
    "Duration of notification creation stages.",
    ["type", "stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
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
    SCHEDULE_ACTION = "schedule_action"


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
    revision: int = 1
    expires_at: datetime | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None


def _managed_origin_conversation_id(
    payload: dict[str, Any] | None,
    *,
    target_conversation_id: str | None = None,
) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("managed_origin_conversation_id")
    if not isinstance(value, str) or not value or value == target_conversation_id:
        return None
    return value


def _notification_visible_in_conversation(
    row: NotificationRow,
    conversation_id: str,
) -> bool:
    if row.conversation_id == conversation_id:
        return True
    return (
        _managed_origin_conversation_id(
            row.payload if isinstance(row.payload, dict) else None,
            target_conversation_id=row.conversation_id,
        )
        == conversation_id
    )


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
        task_queue: QuestionTaskResumer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._pause_waiter = pause_waiter
        self._event_bus = event_bus
        self._providers = providers
        self._question_task_queue = task_queue
        self.cluster_signals: Any = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _run_best_effort(
        self,
        awaitable: Awaitable[None],
        *,
        operation: str,
        notification_id: str,
        timeout: float | None = None,
    ) -> None:
        """Run bounded post-commit work without extending the resolve request."""

        async def _runner() -> None:
            try:
                if timeout is None:
                    await awaitable
                else:
                    await asyncio.wait_for(awaitable, timeout=timeout)
            except Exception:
                logger.warning(
                    "notification: best-effort completion failed",
                    extra={
                        "extra_data": {
                            "notification_id": notification_id,
                            "operation": operation,
                        }
                    },
                    exc_info=True,
                )

        task = asyncio.create_task(_runner(), name=f"notification:{operation}:{notification_id}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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

    async def _publish_notification_cluster_changes(
        self,
        *,
        conversation_id: str,
        user_email: str,
        notification_id: str,
        task_id: str | None,
        session_id: str | None,
        revision: datetime,
        payload: dict[str, Any] | None,
    ) -> None:
        conversation_ids = [conversation_id]
        origin_conversation_id = _managed_origin_conversation_id(
            payload,
            target_conversation_id=conversation_id,
        )
        if origin_conversation_id:
            conversation_ids.append(origin_conversation_id)
        await asyncio.gather(
            *(
                self._publish_cluster_change(
                    conversation_id=scope_conversation_id,
                    user_email=user_email,
                    notification_id=notification_id,
                    task_id=task_id,
                    session_id=session_id,
                    revision=revision,
                )
                for scope_conversation_id in conversation_ids
            )
        )

    async def _publish_schedule_action_cluster_change(
        self,
        *,
        user_email: str,
        conversation_id: str,
        notification_id: str,
        schedule_id: str,
        task_id: str | None,
        revision: datetime,
    ) -> None:
        """Invalidate owner-wide schedule action views on remote controllers."""
        if self.cluster_signals is None:
            return
        from cognis.core.cluster_signals import ClusterSignalKind, ClusterSignalScope

        await self.cluster_signals.publish(
            ClusterSignalKind.SCHEDULE_ACTION_CHANGED,
            scope=ClusterSignalScope(
                owner_token=self.cluster_signals.owner_token(user_email),
                conversation_id=conversation_id,
                schedule_id=schedule_id,
                task_id=task_id,
                notification_id=notification_id,
            ),
            revision=f"{revision.isoformat()}:{notification_id}",
        )

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    _MANAGED_LINK_HOP_CAP = 10

    async def create_question(
        self,
        *,
        origin: QuestionOrigin,
        user_email: str,
        notification_id: str,
        payload: dict[str, Any],
        deliver: bool = True,
    ) -> Notification:
        """Pin the responsible agent separately from the user's delivery destination."""
        if origin.task_id is None and origin.step_run_id:
            async with self._session_factory() as db:
                step_run = await get_step_run(db, origin.step_run_id)
                if step_run is not None:
                    task = await get_task(db, step_run.task_id)
                    if task is not None and task.created_by == user_email:
                        origin = origin.model_copy(update={"task_id": task.task_id})
        user_conversation_id = (
            await self.resolve_target_conversation(
                origin.task_id,
                origin.conversation_id,
                session_id=origin.session_id,
                user_email=user_email,
            )
        ) or origin.conversation_id
        respondent: str | None = None
        async with self._session_factory() as db:
            session = await get_session_row(db, origin.session_id)
            if (
                session is not None
                and session.user_email == user_email
                and session.parent_session_id
            ):
                parent = await get_session_row(db, session.parent_session_id)
                if parent is not None and parent.user_email == user_email:
                    respondent = parent.conversation_id
            if respondent is None:
                link = await get_managed_conversation_link_for_target(db, origin.conversation_id)
                if link is not None and link.conversation_state == "open":
                    respondent = link.controller_conversation_id
            if origin.task_id:
                task = await get_task(db, origin.task_id)
                if task is not None and task.created_by == user_email:
                    origin = origin.model_copy(
                        update={
                            "task_title": task.title,
                            "task_attempt_number": task.attempt_number,
                            "source_type": task.source_type,
                            "source_ref": task.source_ref,
                            "source_session_id": task.source_session_id,
                        }
                    )
                    if respondent is None or respondent == origin.conversation_id:
                        respondent = (
                            task.source_ref
                            if task.source_type in {"chat", "agent"} and task.source_ref
                            else user_conversation_id
                        )
            if respondent:
                conversation = await get_conversation(db, respondent)
                if conversation is None or conversation.user_email != user_email:
                    respondent = None
                elif respondent == origin.conversation_id and not (
                    session is not None and session.parent_session_id
                ):
                    # Never ask the execution currently blocked on this request.
                    respondent = None
        enriched = {
            **payload,
            "question_origin": origin.model_dump(mode="json"),
            "question_delivery_ready": deliver,
        }
        if respondent:
            enriched["question_routing"] = QuestionRouting(
                respondent_conversation_id=respondent,
                user_conversation_id=user_conversation_id,
            ).model_dump(mode="json")
        notification = await self.create(
            notification_type=AGENT_QUESTION if respondent else NotificationType.STEP_QUESTION,
            user_email=user_email,
            conversation_id=respondent or origin.conversation_id,
            task_id=origin.task_id,
            step_name=origin.step_name,
            step_run_id=origin.step_run_id,
            session_id=origin.session_id,
            notification_id=notification_id,
            payload=enriched,
            suppress_event=bool(respondent),
        )
        if respondent and deliver:
            await self.deliver_question(notification)
        return notification

    async def activate_question(self, question_id: str) -> None:
        """Publish only after the requesting execution has persisted its pause."""
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, question_id)
            if row is None or row.notification_type != AGENT_QUESTION or row.status != "pending":
                return
            if not (row.payload or {}).get("question_delivery_ready"):
                result = await db.execute(
                    update(NotificationRow)
                    .where(
                        NotificationRow.notification_id == question_id,
                        NotificationRow.status == "pending",
                        NotificationRow.notification_type == AGENT_QUESTION,
                        NotificationRow.revision == _notification_revision(row),
                    )
                    .values(payload={**(row.payload or {}), "question_delivery_ready": True})
                )
                if not int(getattr(result, "rowcount", 0) or 0):
                    return
                await db.commit()
                await db.refresh(row)
            notification = _row_to_notification(row)
        await self.deliver_question(notification)

    async def deliver_question(self, notification: Notification) -> None:
        """Deliver a correlated question through durable, serialized follow-up admission."""
        from cognis.core.followups import (
            FollowUpMode,
            FollowUpOriginKind,
            FollowUpRelevanceHint,
            FollowUpRequiredAction,
            FollowUpStatus,
            QuestionFollowUp,
            build_follow_up_id,
        )

        if (
            notification.notification_type != AGENT_QUESTION
            or notification.status != "pending"
            or not notification.payload.get("question_delivery_ready")
        ):
            return
        follow_up = QuestionFollowUp(
            follow_up_id=build_follow_up_id(
                kind="question",
                conversation_id=notification.conversation_id,
                parts={"question_id": notification.notification_id},
            ),
            mode=FollowUpMode.INTEGRATE,
            origin_kind=FollowUpOriginKind.QUESTION,
            relevance_hint=FollowUpRelevanceHint.SAME_THREAD,
            required_action=FollowUpRequiredAction.ANSWER_QUESTION,
            status=FollowUpStatus.PAUSED,
            question_id=notification.notification_id,
            revision=notification.revision,
            origin=QuestionOrigin.model_validate(notification.payload["question_origin"]),
            questions=normalize_questions(notification.payload["questions"]),
            context=notification.payload.get("context"),
        )
        await self._event_bus.publish(
            Event(
                type=EventType.FOLLOW_UP_TURN_REQUESTED,
                data={
                    "conversation_id": notification.conversation_id,
                    "follow_up": follow_up.model_dump(mode="json"),
                    "channel_deliverable": False,
                },
            )
        )

    async def recover_questions(self, *, limit: int = 100) -> None:
        """Recover missed delivery; unanswered terminal mediation falls back to the user."""
        from cognis.core.followups import build_follow_up_id
        from cognis.store.models import FollowUpIntentRow

        async with self._session_factory() as db:
            rows = list(
                (
                    await db.execute(
                        select(NotificationRow)
                        .where(
                            NotificationRow.notification_type == AGENT_QUESTION,
                            NotificationRow.status == "pending",
                        )
                        .order_by(NotificationRow.created_at)
                        .limit(limit)
                    )
                ).scalars()
            )
        for row in rows:
            async with self._session_factory() as db:
                origin_live = await self._question_origin_is_live(db, row)
            if not origin_live:
                await self.mark_orphaned(row.notification_id, reason="origin_execution_terminal")
                continue
            if _is_expired_notification(row, now=datetime.now(UTC)):
                await self.mark_orphaned(row.notification_id, reason="expired")
                continue
            if not (row.payload or {}).get("question_delivery_ready"):
                # A crash after pause persistence but before activation is recoverable.
                # Before that commit, only the originating execution may activate it.
                async with self._session_factory() as db:
                    task = await get_task(db, row.task_id) if row.task_id else None
                    state = (task.workflow_state or {}) if task is not None else {}
                if (
                    state.get("pending_pause_type") == AGENT_QUESTION
                    and (state.get("pending_pause_payload") or {}).get("pause_id")
                    == row.notification_id
                ):
                    await self.activate_question(row.notification_id)
                continue
            follow_up_id = build_follow_up_id(
                kind="question",
                conversation_id=row.conversation_id,
                parts={"question_id": row.notification_id},
            )
            async with self._session_factory() as db:
                intent = (
                    await db.execute(
                        select(FollowUpIntentRow).where(
                            FollowUpIntentRow.conversation_id == row.conversation_id,
                            FollowUpIntentRow.follow_up_id == follow_up_id,
                        )
                    )
                ).scalar_one_or_none()
            if intent is None:
                await self.deliver_question(_row_to_notification(row))
            elif intent.status in {"submitted", "failed"}:
                await self.forward_question(
                    row.notification_id,
                    user_email=row.user_email,
                    conversation_id=row.conversation_id,
                    expected_revision=_notification_revision(row),
                )

        if self._question_task_queue is not None:
            async with self._session_factory() as db:
                answered = list(
                    (
                        await db.execute(
                            select(NotificationRow)
                            .join(Task, NotificationRow.task_id == Task.task_id)
                            .where(
                                NotificationRow.notification_type == AGENT_QUESTION,
                                NotificationRow.status == "resolved",
                                Task.status == "paused",
                                Task.workflow_state["pending_pause_payload"]["pause_id"].as_string()
                                == NotificationRow.notification_id,
                            )
                            .limit(limit)
                        )
                    ).scalars()
                )
            for question in answered:
                await self._resume_answered_question(_row_to_notification(question))

    async def _resume_answered_question(self, notification: Notification) -> None:
        queue = self._question_task_queue
        if queue is None or not notification.task_id or queue.has_active_run(notification.task_id):
            return
        reply = notification.resolution or {}
        if reply.get("decision") != "continue" or reply.get("mode") != "structured":
            return
        async with self._session_factory() as db:
            task = await db.get(Task, notification.task_id, with_for_update=True)
            if task is None or task.status != "paused":
                return
            origin = QuestionOrigin.model_validate(notification.payload["question_origin"])
            if origin.task_attempt_number != task.attempt_number:
                return
            state = dict(task.workflow_state or {})
            payload = dict(state.get("pending_pause_payload") or {})
            if payload.get("pause_id") != notification.notification_id:
                return
            payload.update(answers=reply["answers"], mode=reply["mode"])
            state.update(
                pending_pause_type="step_input",
                pending_pause_payload=payload,
                version=int(state.get("version") or 0) + 1,
            )
            task.workflow_state = state
            await db.commit()
        try:
            await queue.resume_task(notification.task_id)
        except ValueError:
            # Capacity/ownership may still belong to another controller.
            # The durable answered pause remains eligible for the next recovery scan.
            logger.info(
                "question: recovered task admission deferred",
                extra={"extra_data": {"task_id": notification.task_id}},
            )

    async def answer_question(
        self,
        question_id: str,
        *,
        user_email: str,
        conversation_id: str,
        expected_revision: int,
        answers: list[dict[str, Any]],
    ) -> bool:
        """Answer only an agent-mediated question assigned to this conversation."""
        from cognis.core.question_sets import validate_reply_for_questions

        async with self._session_factory() as db:
            row = await db.get(NotificationRow, question_id)
            if (
                row is None
                or row.user_email != user_email
                or row.conversation_id != conversation_id
                or row.notification_type != AGENT_QUESTION
            ):
                raise ValueError("Question not found or not assigned to this conversation.")
            reply = validate_reply_for_questions(
                {"mode": "structured", "answers": answers},
                normalize_questions((row.payload or {}).get("questions")),
            )
        reply["answered_by"] = {
            "kind": "agent",
            "conversation_id": conversation_id,
        }

        async def guard(db: AsyncSession) -> bool:
            current = await db.get(NotificationRow, question_id)
            return bool(
                current is not None
                and current.user_email == user_email
                and current.conversation_id == conversation_id
                and current.notification_type == AGENT_QUESTION
                and bool((current.payload or {}).get("question_delivery_ready"))
                and (
                    current.status == "resolved" or await self._question_origin_is_live(db, current)
                )
            )

        resolved = await self.resolve(
            question_id,
            "continue",
            reply,
            admission_guard=guard,
            expected_revision=expected_revision,
        )
        if resolved:
            notification = await self.get(question_id)
            if notification is not None:
                await self._resume_answered_question(notification)
        return resolved

    async def _question_origin_is_live(self, db: AsyncSession, row: NotificationRow) -> bool:
        if row.session_id:
            origin_session = await get_session_row(db, row.session_id)
            if origin_session is None or origin_session.status in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return False
        if row.task_id:
            task = await get_task(db, row.task_id)
            if task is None or task.status in {"completed", "failed", "cancelled"}:
                return False
            origin = (row.payload or {}).get("question_origin") or {}
            if origin.get("task_attempt_number") != task.attempt_number:
                return False
        return True

    async def forward_question(
        self,
        question_id: str,
        *,
        user_email: str,
        conversation_id: str,
        expected_revision: int,
    ) -> bool:
        """Expose the same paused request to the user without creating another waiter."""
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, question_id)
            if (
                row is None
                or row.user_email != user_email
                or row.notification_type != AGENT_QUESTION
                or row.conversation_id != conversation_id
                or row.status != "pending"
                or not (row.payload or {}).get("question_delivery_ready")
                or _notification_revision(row) != expected_revision
                or _is_expired_notification(row, now=datetime.now(UTC))
            ):
                return False
            routing = QuestionRouting.model_validate((row.payload or {}).get("question_routing"))
            # Match task lifecycle lock order: task first, notification CAS second.
            task = await db.get(Task, row.task_id, with_for_update=True) if row.task_id else None
            if not await self._question_origin_is_live(db, row):
                return False
            target = await get_conversation(db, routing.user_conversation_id)
            if target is None or target.user_email != user_email:
                return False
            payload = {**(row.payload or {}), "forwarded_by_conversation_id": conversation_id}
            result = await db.execute(
                update(NotificationRow)
                .where(
                    NotificationRow.notification_id == question_id,
                    NotificationRow.notification_type == AGENT_QUESTION,
                    NotificationRow.status == "pending",
                    NotificationRow.revision == expected_revision,
                )
                .values(
                    notification_type=NotificationType.STEP_QUESTION,
                    conversation_id=routing.user_conversation_id,
                    payload=payload,
                    revision=expected_revision + 1,
                )
            )
            if not int(getattr(result, "rowcount", 0) or 0):
                await db.rollback()
                return False
            if row.task_id:
                state = dict(task.workflow_state or {}) if task is not None else {}
                pending_payload = state.get("pending_pause_payload") or {}
                if task is not None and pending_payload.get("pause_id") == question_id:
                    state["pending_pause_type"] = "step_input"
                    state["version"] = int(state.get("version") or 0) + 1
                    task.workflow_state = state
            await db.commit()
            await db.refresh(row)
            notification = _row_to_notification(row)
        self._pause_waiter.retarget(
            question_id,
            pause_type=NotificationType.STEP_QUESTION,
            conversation_id=notification.conversation_id,
        )
        await self._event_bus.publish(
            Event(
                type=EventType.NOTIFICATION_CREATED,
                data={
                    "notification_id": question_id,
                    "notification_type": NotificationType.STEP_QUESTION,
                    "user_email": user_email,
                    "conversation_id": notification.conversation_id,
                    "task_id": notification.task_id,
                    "step_name": notification.step_name,
                    "session_id": notification.session_id,
                    "payload": notification.payload,
                },
            )
        )
        await self._publish_notification_cluster_changes(
            conversation_id=notification.conversation_id,
            user_email=user_email,
            notification_id=question_id,
            task_id=notification.task_id,
            session_id=notification.session_id,
            revision=datetime.now(UTC),
            payload=payload,
        )
        return True

    async def resolve_target_conversation(
        self,
        task_id: str | None,
        conversation_id: str | None,
        *,
        session_id: str | None = None,
        user_email: str | None = None,
    ) -> str | None:
        """Resolve the user-facing conversation for a notification.

        For task-originated notifications, the target is resolved using
        the task's delivery settings — the same logic used for task result
        delivery.  This ensures escalations, gates, and step questions
        from scheduled tasks reach the user via the configured channel.

        For non-task child-session notifications, the target first follows
        ``Session.parent_session_id`` to the root session's conversation.
        This remains correct when a delegated child has a distinct
        conversation_id.

        The target is then walked up the ``ManagedConversationLink`` chain
        until a conversation with no open link is found (i.e. the
        channel-bound parent).  This covers the delegate → managed conv →
        parent conv chain.
        Up to ``_MANAGED_LINK_HOP_CAP`` hops are followed to handle
        nested managed conversations; a visited-set prevents cycles.

        For direct-chat notifications, the conversation_id is used as-is.
        """
        if not task_id:
            managed_conversation_id = await self._resolve_managed_conversation_chain(
                conversation_id
            )
            if managed_conversation_id != conversation_id:
                return managed_conversation_id
            session_conversation_id = await self._resolve_parent_session_conversation(
                session_id=session_id,
                user_email=user_email,
                fallback_conversation_id=conversation_id,
            )
            return await self._resolve_managed_conversation_chain(session_conversation_id)

        async with self._session_factory() as db:
            task_row = await get_task(db, task_id)
            if task_row is None:
                return conversation_id

            delivery_mode = task_row.delivery_mode or "same_conversation"

            if delivery_mode == "same_conversation":
                # Agent-created tasks also store the originating conversation
                # in source_ref. Keep interactive prompts on that conversation.
                if task_row.source_type in {"chat", "agent"} and task_row.source_ref:
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

    async def _resolve_parent_session_conversation(
        self,
        *,
        session_id: str | None,
        user_email: str | None,
        fallback_conversation_id: str | None,
    ) -> str | None:
        """Resolve a delegated session lineage to its root conversation."""
        if not session_id:
            return fallback_conversation_id

        candidate_session_id = session_id
        candidate_conversation_id = fallback_conversation_id
        visited: set[str] = set()
        for _ in range(self._MANAGED_LINK_HOP_CAP):
            if candidate_session_id in visited:
                logger.warning(
                    "notification: parent session cycle detected",
                    extra={"extra_data": {"session_id": session_id}},
                )
                break
            visited.add(candidate_session_id)
            async with self._session_factory() as db:
                session_row = await get_session_row(db, candidate_session_id)
            if session_row is None or (
                user_email is not None and session_row.user_email != user_email
            ):
                break
            candidate_conversation_id = session_row.conversation_id
            if not session_row.parent_session_id:
                break
            candidate_session_id = session_row.parent_session_id
        return candidate_conversation_id

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
        *,
        session_id: str | None = None,
        user_email: str | None = None,
    ) -> dict[str, Any]:
        """Return managed-origin metadata for a notification payload.

        If ``conversation_id`` is a managed target, returns a dict with
        ``managed_conversation_title`` and ``managed_target_agent_id``
        from the first link hop.  Returns an empty dict when the
        conversation is not a managed target.
        """
        origin_conversation_id = conversation_id
        async with self._session_factory() as db:
            link = await get_managed_conversation_link_for_target(db, origin_conversation_id)
        if link is None:
            origin_conversation_id = (
                await self._resolve_parent_session_conversation(
                    session_id=session_id,
                    user_email=user_email,
                    fallback_conversation_id=conversation_id,
                )
                or conversation_id
            )
            async with self._session_factory() as db:
                link = await get_managed_conversation_link_for_target(db, origin_conversation_id)
        if link is None:
            return {}
        return {
            "managed_origin_conversation_id": origin_conversation_id,
            "managed_link_id": link.link_id,
            "managed_conversation_title": link.title or "",
            "managed_target_agent_id": link.target_agent_id or "",
        }

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def upsert_schedule_action(
        self,
        *,
        schedule_id: str,
        user_email: str,
        agent_id: str,
        schedule_name: str,
        consecutive_errors: int,
        error_summary: str,
        auto_disabled: bool,
        task_id: str | None = None,
        reopen_resolved: bool = True,
    ) -> Notification | None:
        """Create or update the single actionable notification for a schedule."""
        notification_id = f"notif_schedule_{schedule_id}"
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            schedule = (
                await db.execute(
                    select(Schedule).where(Schedule.schedule_id == schedule_id).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                schedule is None
                or schedule.created_by != user_email
                or schedule.agent_id != agent_id
                or int(schedule.consecutive_errors or 0) != consecutive_errors
            ):
                return None
            canonical_auto_disabled = not schedule.enabled and str(
                schedule.disabled_reason or ""
            ).startswith("auto_consecutive_failures:")
            if auto_disabled != canonical_auto_disabled:
                return None
            if not canonical_auto_disabled and schedule.last_run_status != "failed":
                return None
            if not schedule.enabled and not canonical_auto_disabled:
                return None
            if schedule.last_terminal_task_id != task_id:
                return None

            conversation = await get_latest_active_conversation_for_agent(db, user_email, agent_id)
            if conversation is None:
                conversation = await get_agent_direct_conversation(db, user_email, agent_id)
            conversation_id = (
                conversation.conversation_id
                if conversation is not None
                else f"schedule:{schedule_id}"
            )
            payload = {
                "schedule_id": schedule_id,
                "schedule_name": schedule_name,
                "severity": "critical" if auto_disabled else "warning",
                "consecutive_errors": consecutive_errors,
                "error_summary": _safe_schedule_error_summary(error_summary),
                "latest_error_at": now.isoformat(),
                "auto_disabled": auto_disabled,
                "action_url": f"/schedules/{schedule_id}",
                "message": (
                    f'Schedule "{schedule_name}" was automatically disabled.'
                    if auto_disabled
                    else f'Schedule "{schedule_name}" failed.'
                ),
            }
            inserted = False
            row = (
                await db.execute(
                    select(NotificationRow)
                    .where(NotificationRow.notification_id == notification_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                row = NotificationRow(
                    notification_id=notification_id,
                    notification_type=NotificationType.SCHEDULE_ACTION,
                    user_email=user_email,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    payload=payload,
                    status="pending",
                    created_at=now,
                )
                db.add(row)
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    row = (
                        await db.execute(
                            select(NotificationRow)
                            .where(NotificationRow.notification_id == notification_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        raise
                else:
                    inserted = True
                    NOTIFICATIONS_CREATED.labels(type=NotificationType.SCHEDULE_ACTION).inc()
            if row.user_email != user_email:
                raise ValueError("Schedule notification owner mismatch")
            existing_payload = dict(row.payload or {})
            from cognis.api.dashboard_issues import schedule_incident_token

            incident_token = schedule_incident_token(
                schedule_id,
                task_id,
                schedule.last_fired_at,
            )
            payload["incident_token"] = incident_token
            if (
                not inserted
                and row.status == "resolved"
                and existing_payload.get("incident_token") == incident_token
            ):
                return Notification(
                    notification_id=notification_id,
                    notification_type=NotificationType.SCHEDULE_ACTION,
                    user_email=user_email,
                    conversation_id=row.conversation_id,
                    task_id=row.task_id,
                    payload=existing_payload,
                    status="resolved",
                    resolution=row.resolution,
                    created_at=row.created_at,
                    resolved_at=row.resolved_at,
                )
            if (
                not inserted
                and row.status == "resolved"
                and not reopen_resolved
                and (
                    existing_payload.get("incident_token") is None
                    or (task_id is None and schedule.last_fired_at is None)
                )
            ):
                return Notification(
                    notification_id=notification_id,
                    notification_type=NotificationType.SCHEDULE_ACTION,
                    user_email=user_email,
                    conversation_id=row.conversation_id,
                    task_id=row.task_id,
                    payload=existing_payload,
                    status="resolved",
                    resolution=row.resolution,
                    created_at=row.created_at,
                    resolved_at=row.resolved_at,
                )
            if (
                not inserted
                and row.status == "pending"
                and existing_payload.get("auto_disabled") is True
            ):
                payload["auto_disabled"] = True
                payload["severity"] = "critical"
                payload["consecutive_errors"] = max(
                    int(existing_payload.get("consecutive_errors") or 0),
                    consecutive_errors,
                )
            if not inserted:
                row.conversation_id = conversation_id
                row.task_id = task_id
                row.payload = payload
                row.status = "pending"
                row.resolution = None
                row.resolved_at = None
                await db.commit()

        event_data = {
            "notification_id": notification_id,
            "notification_type": NotificationType.SCHEDULE_ACTION,
            "user_email": user_email,
            "conversation_id": conversation_id,
            "task_id": task_id,
            "payload": payload,
        }
        await self._event_bus.publish(
            Event(type=EventType.SCHEDULE_ACTION_CHANGED, data={**event_data, "status": "pending"})
        )
        await self._publish_notification_cluster_changes(
            conversation_id=conversation_id,
            user_email=user_email,
            notification_id=notification_id,
            task_id=task_id,
            session_id=None,
            revision=now,
            payload=payload,
        )
        await self._publish_schedule_action_cluster_change(
            user_email=user_email,
            conversation_id=conversation_id,
            notification_id=notification_id,
            schedule_id=schedule_id,
            task_id=task_id,
            revision=now,
        )
        return Notification(
            notification_id=notification_id,
            notification_type=NotificationType.SCHEDULE_ACTION,
            user_email=user_email,
            conversation_id=conversation_id,
            task_id=task_id,
            payload=payload,
            status="pending",
            created_at=now,
        )

    async def dismiss_schedule_action(
        self,
        schedule_id: str,
        *,
        user_email: str,
        incident_token: str,
    ) -> str:
        """Dismiss the current schedule incident with compare-and-set semantics."""
        from cognis.api.dashboard_issues import schedule_incident_token

        notification_id = f"notif_schedule_{schedule_id}"
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            schedule = (
                await db.execute(
                    select(Schedule).where(Schedule.schedule_id == schedule_id).with_for_update()
                )
            ).scalar_one_or_none()
            if schedule is None or schedule.created_by != user_email:
                return "not_found"
            current_token = schedule_incident_token(
                schedule_id,
                schedule.last_terminal_task_id,
                schedule.last_fired_at,
            )
            if incident_token != current_token:
                return "stale"
            row = (
                await db.execute(
                    select(NotificationRow)
                    .where(NotificationRow.notification_id == notification_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None and row.user_email != user_email:
                return "not_found"
            if row is not None and row.status == "resolved":
                resolved_token = (row.payload or {}).get("incident_token")
                return "dismissed" if resolved_token == current_token else "stale"
            if row is None:
                conversation = await get_latest_active_conversation_for_agent(
                    db, user_email, schedule.agent_id
                )
                if conversation is None:
                    conversation = await get_agent_direct_conversation(
                        db, user_email, schedule.agent_id
                    )
                row = NotificationRow(
                    notification_id=notification_id,
                    notification_type=NotificationType.SCHEDULE_ACTION,
                    user_email=user_email,
                    conversation_id=(
                        conversation.conversation_id
                        if conversation is not None
                        else f"schedule:{schedule_id}"
                    ),
                    task_id=schedule.last_terminal_task_id,
                    payload={
                        "schedule_id": schedule_id,
                        "schedule_name": schedule.name,
                        "incident_token": current_token,
                        "action_url": f"/schedules/{schedule_id}",
                    },
                    status="resolved",
                    resolution={"decision": "dismissed", "reason": "user_dismissed"},
                    created_at=now,
                    resolved_at=now,
                )
                db.add(row)
            else:
                payload = dict(row.payload or {})
                if payload.get("incident_token") not in {None, current_token}:
                    return "stale"
                payload["incident_token"] = current_token
                row.payload = payload
                row.status = "resolved"
                row.resolution = {"decision": "dismissed", "reason": "user_dismissed"}
                row.resolved_at = now
            await db.commit()
            conversation_id = row.conversation_id
            task_id = row.task_id
            payload = dict(row.payload or {})
        await self._event_bus.publish(
            Event(
                type=EventType.SCHEDULE_ACTION_CHANGED,
                data={
                    "notification_id": notification_id,
                    "notification_type": NotificationType.SCHEDULE_ACTION,
                    "user_email": user_email,
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "payload": payload,
                    "decision": "dismissed",
                    "status": "resolved",
                },
            )
        )
        await self._publish_notification_cluster_changes(
            conversation_id=conversation_id,
            user_email=user_email,
            notification_id=notification_id,
            task_id=task_id,
            session_id=None,
            revision=now,
            payload=payload,
        )
        await self._publish_schedule_action_cluster_change(
            user_email=user_email,
            conversation_id=conversation_id,
            notification_id=notification_id,
            schedule_id=schedule_id,
            task_id=task_id,
            revision=now,
        )
        return "dismissed"

    async def resolve_schedule_action(
        self,
        schedule_id: str,
        *,
        user_email: str,
        reason: str,
        expected_terminal_task_id: str | None = None,
        require_canonical_success: bool = False,
    ) -> bool:
        """Resolve a schedule notification after recovery or decommissioning."""
        notification_id = f"notif_schedule_{schedule_id}"
        now = datetime.now(UTC)
        async with self._session_factory() as db:
            if require_canonical_success:
                schedule = (
                    await db.execute(
                        select(Schedule)
                        .where(Schedule.schedule_id == schedule_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    schedule is None
                    or schedule.created_by != user_email
                    or schedule.last_run_status != "success"
                    or not schedule.enabled
                    or str(schedule.disabled_reason or "").startswith("auto_consecutive_failures:")
                    or schedule.last_terminal_task_id != expected_terminal_task_id
                ):
                    return False
            row = await db.get(NotificationRow, notification_id)
            if row is None or row.user_email != user_email:
                return False
            conversation_id = row.conversation_id
            task_id = row.task_id
            payload = dict(row.payload or {})
            result = await db.execute(
                update(NotificationRow)
                .where(
                    NotificationRow.notification_id == notification_id,
                    NotificationRow.user_email == user_email,
                    NotificationRow.status == "pending",
                )
                .values(
                    status="resolved",
                    resolution={"decision": "resolved", "reason": reason},
                    resolved_at=now,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                await db.rollback()
                return False
            await db.commit()
        await self._event_bus.publish(
            Event(
                type=EventType.SCHEDULE_ACTION_CHANGED,
                data={
                    "notification_id": notification_id,
                    "notification_type": NotificationType.SCHEDULE_ACTION,
                    "user_email": user_email,
                    "conversation_id": conversation_id,
                    "task_id": task_id,
                    "payload": payload,
                    "decision": "resolved",
                    "status": "resolved",
                },
            )
        )
        await self._publish_notification_cluster_changes(
            conversation_id=conversation_id,
            user_email=user_email,
            notification_id=notification_id,
            task_id=task_id,
            session_id=None,
            revision=now,
            payload=payload,
        )
        await self._publish_schedule_action_cluster_change(
            user_email=user_email,
            conversation_id=conversation_id,
            notification_id=notification_id,
            schedule_id=schedule_id,
            task_id=task_id,
            revision=now,
        )
        NOTIFICATIONS_RESOLVED.labels(
            type=NotificationType.SCHEDULE_ACTION, decision="resolved"
        ).inc()
        return True

    async def reconcile_schedule_actions(self, *, limit: int = 500) -> int:
        """Recreate notifications from durable actionable schedule state."""
        async with self._session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(Schedule)
                        .where(
                            or_(
                                and_(
                                    Schedule.last_run_status == "failed",
                                    Schedule.enabled.is_(True),
                                ),
                                Schedule.disabled_reason.like("auto_consecutive_failures:%"),
                            ),
                        )
                        .order_by(Schedule.updated_at.desc(), Schedule.schedule_id)
                        .limit(limit)
                    )
                ).all()
            )
        for row in rows:
            await self.upsert_schedule_action(
                schedule_id=row.schedule_id,
                user_email=row.created_by,
                agent_id=row.agent_id,
                schedule_name=row.name,
                consecutive_errors=int(row.consecutive_errors or 0),
                error_summary="Scheduled run failed",
                auto_disabled=(
                    not row.enabled
                    and str(row.disabled_reason or "").startswith("auto_consecutive_failures:")
                ),
                task_id=row.last_terminal_task_id,
                reopen_resolved=False,
            )
        return len(rows)

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
            conversation_id
            if notification_type == AGENT_QUESTION
            else (
                await self.resolve_target_conversation(
                    task_id,
                    conversation_id,
                    session_id=session_id,
                    user_email=user_email,
                )
            )
            or conversation_id
        )

        # When the notification originated inside a managed sub-conversation
        # and was redirected to the parent, enrich the payload with managed-
        # origin metadata so channel renderers can surface the context.
        enriched_payload = dict(payload or {})
        if resolved_conversation_id != conversation_id and not task_id:
            origin_meta = await self.resolve_managed_origin_metadata(
                conversation_id,
                session_id=session_id,
                user_email=user_email,
            )
            if origin_meta:
                enriched_payload = {**enriched_payload, **origin_meta}
        expires_at = _notification_payload_expires_at(
            notification_type,
            enriched_payload,
            created_at=now,
        )

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
            revision=1,
            expires_at=expires_at,
            created_at=now,
        )

        # Persist before any delivery so every observer can recover from the
        # durable notification row.
        commit_started = monotonic()
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
                    revision=1,
                    expires_at=expires_at,
                    created_at=now,
                )
            )
            await db.commit()
        NOTIFICATION_CREATE_STAGE_DURATION.labels(
            type=notification_type,
            stage="commit",
        ).observe(monotonic() - commit_started)

        self._run_best_effort(
            self._publish_notification_cluster_changes(
                conversation_id=resolved_conversation_id,
                user_email=user_email,
                notification_id=nid,
                task_id=task_id,
                session_id=session_id,
                revision=now,
                payload=enriched_payload,
            ),
            operation="create_cluster_publish",
            notification_id=nid,
            timeout=3.0,
        )

        if not _is_callback_only_notification(notification_type, enriched_payload):
            # Register PauseWaiter so the blocking coroutine can be resolved.
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
            # A remote controller can resolve the durable row between the commit
            # and local waiter registration. Replay that terminal state locally so
            # the owner does not wait for the next polling interval.
            registered_row = None
            async with self._session_factory() as db:
                get_row = getattr(db, "get", None)
                if callable(get_row):
                    registered_row = await get_row(NotificationRow, nid)
            if (
                registered_row is not None
                and registered_row.status == "resolved"
                and isinstance(registered_row.resolution, dict)
            ):
                self._pause_waiter.resolve(
                    nid,
                    PauseResolution(
                        decision=str(registered_row.resolution.get("decision") or "deny"),
                        data=_resolution_waiter_data(registered_row.resolution, {}),
                    ),
                )

        # Local delivery and HA invalidation are independent post-commit work.
        # A slow PostgreSQL NOTIFY or unrelated EventBus subscriber must not
        # delay waiter registration or the caller entering the pause.
        if not suppress_event:
            self._run_best_effort(
                self._event_bus.publish(
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
                            "expires_at": expires_at.isoformat() if expires_at else None,
                        },
                    )
                ),
                operation="create_local_event",
                notification_id=nid,
                timeout=3.0,
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
        expected_revision: int | None = None,
        data_factory: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        selection_guard: ColumnElement[bool] | None = None,
        pre_submit_guard: Callable[[], bool] | None = None,
        human_answer: bool = False,
    ) -> bool:
        """Resolve a notification, update DB, resolve PauseWaiter.

        Returns True if the notification was resolved. Duplicate resolves for
        an already-resolved notification are treated as successful when they
        repeat the same decision and resolution payload; this keeps UI
        retries/idempotent WebSocket frames from surfacing a false "not found"
        error after an approval raced with remote Intaris reconciliation,
        without accepting conflicting duplicate input.
        """
        resolution_data = dict(data or {})
        # Only authenticated ingress can assert human authorship.
        resolution_data.pop("human_answer", None)
        if human_answer:
            if user_email is None:
                raise ValueError("Human question answers require an authenticated user")
            resolution_data["human_answer"] = True
        if user_email is not None:
            async with self._session_factory() as db:
                question = await db.get(NotificationRow, notification_id)
                if question is not None and question.notification_type == AGENT_QUESTION:
                    return False
        now = datetime.now(UTC)
        claim_token = f"resolve_{uuid.uuid4().hex}"
        claim_revision = 0
        owns_claim = False
        waits_for_existing_claim = False

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
            if user_email is not None and row.user_email != user_email:
                return False
            if row.notification_type == NotificationType.GATE and row.task_id:
                # Enforce current gate identity and deadline in the decision CAS.
                is_timeout = user_email is None and resolution_data.get("reason") == "timeout"
                offered = {
                    str(option.get("action"))
                    for option in effective_gate_options((row.payload or {}).get("options"))
                    if isinstance(option, dict)
                }
                timeout_action = (row.payload or {}).get("timeout_action")
                if is_timeout:
                    if decision not in {"fail", "continue", "cancel"}:
                        return False
                    if timeout_action is not None and decision != timeout_action:
                        return False
                elif decision not in offered:
                    return False
                active_gate = [
                    Task.task_id == row.task_id,
                    Task.status.in_(["running", "paused"]),
                    Task.workflow_state["pending_pause_type"].as_string() == "gate",
                    Task.workflow_state["pending_pause_payload"]["pause_id"].as_string()
                    == row.notification_id,
                ]
                task_attempt = (row.payload or {}).get("task_attempt")
                if isinstance(task_attempt, int):
                    active_gate.append(Task.attempt_number == task_attempt)
                gate_guard = select(Task.task_id).where(*active_gate).exists()
                deadline_guard: ColumnElement[bool]
                if row.status in {"resolving", "resolved"}:
                    # The deadline governs admission, not completion of an
                    # already-admitted winner after its controller disappears.
                    if not _resolution_claim_matches(
                        row.resolution or {},
                        decision,
                        resolution_data,
                        require_exact_payload=True,
                    ):
                        return False
                    deadline_guard = true()
                else:
                    deadline_guard = _unexpired_notification_predicate(row, db)
                    if is_timeout:
                        deadline_guard = ~deadline_guard
                selection_guard = (
                    gate_guard
                    & deadline_guard
                    & (selection_guard if selection_guard is not None else true())
                )
                if not await db.scalar(select(gate_guard)):
                    return False
            if (
                row.notification_type
                in {NotificationType.ESCALATION, NotificationType.AUTH_CHALLENGE}
                and row.status == "pending"
                and _is_expired_notification(row, now=now)
                and not (
                    row.notification_type == NotificationType.ESCALATION
                    and user_email is None
                    and decision == "deny"
                    and resolution_data.get("reason") == "timeout"
                )
            ):
                return False
            if row.status == "resolved":
                if row.notification_type == NotificationType.STEP_QUESTION and not (
                    _question_resolution_matches(row.resolution, decision, resolution_data)
                ):
                    return False
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
            row_revision = _notification_revision(row)
            current_resolution = row.resolution if isinstance(row.resolution, dict) else {}
            same_inflight_request = row.status == "resolving" and _resolution_claim_matches(
                current_resolution, decision, resolution_data
            )
            if (
                expected_revision is not None
                and row_revision != expected_revision
                and not same_inflight_request
            ):
                return False
            if row.status == "resolving":
                existing = row.resolution if isinstance(row.resolution, dict) else {}
                if row.notification_type == NotificationType.STEP_QUESTION and not (
                    _question_resolution_matches(existing, decision, resolution_data)
                ):
                    return False
                if str(existing.get("decision") or "").lower() != decision.lower():
                    return False
                claimed_at = row.resolved_at
                if claimed_at is not None:
                    claimed_at = (
                        claimed_at
                        if claimed_at.tzinfo is not None
                        else claimed_at.replace(tzinfo=UTC)
                    )
                if (
                    claimed_at is None
                    or (now - claimed_at).total_seconds() < _RESOLUTION_CLAIM_SECONDS
                ):
                    if not _resolution_claim_matches(existing, decision, resolution_data):
                        return False
                    waits_for_existing_claim = True
                else:
                    if row.notification_type in {
                        NotificationType.ESCALATION,
                        NotificationType.AUTH_CHALLENGE,
                    } and _is_expired_notification(row, now=datetime.now(UTC)):
                        return False
                    result = await db.execute(
                        update(NotificationRow)
                        .where(
                            NotificationRow.notification_id == notification_id,
                            NotificationRow.status == "resolving",
                            NotificationRow.resolved_at == row.resolved_at,
                            NotificationRow.revision == row_revision,
                            selection_guard if selection_guard is not None else true(),
                            _unexpired_notification_predicate(row, db)
                            if row.notification_type
                            in {NotificationType.ESCALATION, NotificationType.AUTH_CHALLENGE}
                            else true(),
                        )
                        .values(
                            resolution={
                                "decision": decision,
                                "state": "submitting",
                                "claim_token": claim_token,
                                **resolution_data,
                            },
                            resolved_at=now,
                            revision=row_revision + 1,
                        )
                    )
                    await db.commit()
                    owns_claim = _claim_update_succeeded(
                        result,
                        row=row,
                        claim_token=claim_token,
                    )
                    if not owns_claim:
                        return False
                    claim_revision = row_revision + 1
            elif row.status == "pending":
                result = await db.execute(
                    update(NotificationRow)
                    .where(
                        NotificationRow.notification_id == notification_id,
                        NotificationRow.status == "pending",
                        NotificationRow.revision == row_revision,
                        selection_guard if selection_guard is not None else true(),
                        _unexpired_notification_predicate(row, db)
                        if row.notification_type
                        in {NotificationType.ESCALATION, NotificationType.AUTH_CHALLENGE}
                        and not (
                            row.notification_type == NotificationType.ESCALATION
                            and user_email is None
                            and decision == "deny"
                            and resolution_data.get("reason") == "timeout"
                        )
                        else true(),
                    )
                    .values(
                        status="resolving",
                        resolution={
                            "decision": decision,
                            "state": "submitting",
                            "claim_token": claim_token,
                            **resolution_data,
                        },
                        resolved_at=now,
                        revision=row_revision + 1,
                    )
                    .execution_options(synchronize_session="fetch")
                )
                await db.commit()
                owns_claim = _claim_update_succeeded(
                    result,
                    row=row,
                    claim_token=claim_token,
                )
                if not owns_claim:
                    if selection_guard is not None:
                        return False
                    return await self.resolve(
                        notification_id,
                        decision,
                        resolution_data,
                        user_email=user_email,
                        admission_guard=admission_guard,
                        expected_revision=expected_revision,
                        data_factory=data_factory,
                        selection_guard=selection_guard,
                        pre_submit_guard=pre_submit_guard,
                        human_answer=human_answer,
                    )
                claim_revision = row_revision + 1
            else:
                return False

            notification_type = row.notification_type
            created_at = row.created_at
            row_user_email = row.user_email
            conversation_id = row.conversation_id
            task_id = row.task_id
            step_name = row.step_name
            session_id = row.session_id
            notification_payload = dict(row.payload) if isinstance(row.payload, dict) else {}
            submission_expiry = _notification_expiry(row)
        if waits_for_existing_claim:
            return await self._wait_for_existing_resolution_claim(
                notification_id,
                decision,
                resolution_data,
                user_email=user_email,
                admission_guard=admission_guard,
                selection_guard=selection_guard,
                pre_submit_guard=pre_submit_guard,
                human_answer=human_answer,
            )
        if (
            notification_type == NotificationType.AUTH_CHALLENGE
            and owns_claim
            and submission_expiry is not None
            and datetime.now(UTC) >= submission_expiry
        ):
            # The UPDATE may have waited for a lock past its deadline. Revalidate
            # after acquiring the claim and before admitting any response work.
            await self.mark_orphaned(notification_id, reason="expired", include_resolving=True)
            return False
        if data_factory is not None and owns_claim:
            heartbeat_stop = asyncio.Event()

            async def _refresh_claim() -> None:
                interval = max(1.0, _RESOLUTION_CLAIM_SECONDS / 3)
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval)
                        return
                    except TimeoutError:
                        pass
                    async with self._session_factory() as heartbeat_db:
                        heartbeat_result = await heartbeat_db.execute(
                            update(NotificationRow)
                            .where(
                                NotificationRow.notification_id == notification_id,
                                NotificationRow.status == "resolving",
                                NotificationRow.revision == claim_revision,
                            )
                            .values(resolved_at=datetime.now(UTC))
                        )
                        await heartbeat_db.commit()
                        if not int(getattr(heartbeat_result, "rowcount", 0) or 0):
                            return

            heartbeat_task = asyncio.create_task(_refresh_claim())
            try:
                generated_data = await data_factory()
            except Exception:
                async with self._session_factory() as db:
                    await db.execute(
                        update(NotificationRow)
                        .where(
                            NotificationRow.notification_id == notification_id,
                            NotificationRow.status == "resolving",
                            NotificationRow.revision == claim_revision,
                        )
                        .values(
                            status="pending",
                            resolution=None,
                            resolved_at=None,
                            revision=claim_revision + 1,
                        )
                    )
                    await db.commit()
                raise
            finally:
                heartbeat_stop.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            resolution_data = {**resolution_data, **generated_data}
            async with self._session_factory() as db:
                result = await db.execute(
                    update(NotificationRow)
                    .where(
                        NotificationRow.notification_id == notification_id,
                        NotificationRow.status == "resolving",
                        NotificationRow.revision == claim_revision,
                    )
                    .values(
                        resolution={
                            "decision": decision,
                            "state": "submitting",
                            "claim_token": claim_token,
                            **resolution_data,
                        }
                    )
                )
                await db.commit()
                if not int(getattr(result, "rowcount", 0) or 0):
                    return False
        if (
            owns_claim
            and resolution_data.get("human_answer") is True
            and decision == "continue"
            and notification_type == NotificationType.STEP_QUESTION
        ):
            await self._record_question_answer(
                notification_id=notification_id,
                notification_payload=notification_payload,
                resolution_data=resolution_data,
                session_id=session_id,
                user_email=row_user_email,
            )
        rejection: ResolutionRejected | None = None
        if (
            notification_type == NotificationType.AUTH_CHALLENGE
            and owns_claim
            and submission_expiry is not None
            and datetime.now(UTC) >= submission_expiry
        ):
            await self.mark_orphaned(notification_id, reason="expired", include_resolving=True)
            return False
        if notification_type == NotificationType.ESCALATION and owns_claim:
            if (pre_submit_guard is not None and not pre_submit_guard()) or (
                submission_expiry is not None
                and datetime.now(UTC) >= submission_expiry
                and not (
                    user_email is None
                    and decision == "deny"
                    and resolution_data.get("reason") == "timeout"
                )
            ):
                # The claim is ours and no submission has started. A new local
                # registration may invalidate bare selection during the CAS.
                async with self._session_factory() as db:
                    await db.execute(
                        update(NotificationRow)
                        .where(
                            NotificationRow.notification_id == notification_id,
                            NotificationRow.status == "resolving",
                            NotificationRow.revision == claim_revision,
                        )
                        .values(
                            status="pending",
                            resolution=None,
                            resolved_at=None,
                            revision=claim_revision + 1,
                        )
                    )
                    await db.commit()
                return False
            try:
                with scoped_runtime_context(user_email=user_email or row_user_email):
                    await asyncio.wait_for(
                        self._providers.guardrails.submit_decision(
                            notification_id, decision, resolution_data.get("note")
                        ),
                        timeout=_INTARIS_SUBMISSION_TIMEOUT_SECONDS,
                    )
            except Exception as exc:
                NOTIFICATION_ESCALATION_SUBMIT_FAILURES.inc()
                logger.warning(
                    "notification: escalation decision submit failed",
                    extra={
                        "extra_data": {"notification_id": notification_id, "decision": decision}
                    },
                    # Exception payloads can contain upstream content.
                )
                if isinstance(exc, ResolutionRejected) and (
                    exc.permanent or exc.requires_reevaluation
                ):
                    # This receipt cannot be approved. Close the owned wait:
                    # stale context is cancellation, not a human refusal.
                    rejection = exc
                    decision = "cancel" if exc.requires_reevaluation else "deny"
                    resolution_data = {
                        "note": exc.message,
                        "reason": exc.code,
                        "resolution_rejected": True,
                        **({"requires_reevaluation": True} if exc.requires_reevaluation else {}),
                    }
                elif not isinstance(exc, TimeoutError):
                    async with self._session_factory() as db:
                        await db.execute(
                            update(NotificationRow)
                            .where(
                                NotificationRow.notification_id == notification_id,
                                NotificationRow.status == "resolving",
                                NotificationRow.revision == claim_revision,
                            )
                            .values(
                                status="pending",
                                resolution=None,
                                resolved_at=None,
                                revision=claim_revision + 1,
                            )
                        )
                        await db.commit()
                    if isinstance(exc, ResolutionRejected):
                        raise
                    return False
                else:
                    return False

        async with self._session_factory() as db:
            if admission_guard is not None and not await admission_guard(db):
                await db.rollback()
                return False
            current = await db.get(NotificationRow, notification_id)
            if current is not None and current.status == "resolved":
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
                    NotificationRow.status == "resolving",
                    NotificationRow.revision == claim_revision,
                )
                .values(
                    status="resolved",
                    resolution={"decision": decision, "state": "resolved", **resolution_data},
                    resolved_at=now,
                    revision=claim_revision + 1,
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

        # Publish the authoritative terminal state locally before scheduling
        # best-effort cross-controller and timeline mirrors.
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
                    "resumes_execution": True,
                    "managed_origin_conversation_id": _managed_origin_conversation_id(
                        notification_payload,
                        target_conversation_id=conversation_id,
                    ),
                },
            )
        )
        self._run_best_effort(
            self._publish_notification_cluster_changes(
                conversation_id=conversation_id,
                user_email=row_user_email,
                notification_id=notification_id,
                task_id=task_id,
                session_id=session_id,
                revision=now,
                payload=notification_payload,
            ),
            operation="cluster_publish",
            notification_id=notification_id,
            timeout=_CLUSTER_PUBLISH_TIMEOUT_SECONDS,
        )
        self._run_best_effort(
            self._record_user_interaction(
                notification_id=notification_id,
                notification_type=str(notification_type),
                notification_payload=notification_payload,
                resolution_data=resolution_data,
                decision=decision,
                session_id=session_id,
                user_email=row_user_email,
            ),
            operation="interaction_record",
            notification_id=notification_id,
            timeout=_INTERACTION_RECORD_TIMEOUT_SECONDS,
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
        if rejection is not None:
            rejection.resolved_locally = True
            raise rejection
        return True

    async def _wait_for_existing_resolution_claim(
        self,
        notification_id: str,
        decision: str,
        resolution_data: dict[str, Any],
        *,
        user_email: str | None,
        admission_guard: Callable[[Any], Awaitable[bool]] | None,
        selection_guard: ColumnElement[bool] | None = None,
        pre_submit_guard: Callable[[], bool] | None = None,
        human_answer: bool = False,
    ) -> bool:
        """Wait until another resolver reaches an authoritative terminal state."""

        deadline = monotonic() + _RESOLUTION_CLAIM_SECONDS + _RESOLUTION_COMPLETION_GRACE_SECONDS
        while True:
            async with self._session_factory() as db:
                if admission_guard is not None and not await admission_guard(db):
                    await db.rollback()
                    return False
                row = await db.get(NotificationRow, notification_id)
                if row is None or (user_email is not None and row.user_email != user_email):
                    return False
                if row.status == "resolved":
                    if not _is_same_resolution(row, decision, resolution_data):
                        return False
                    self._pause_waiter.resolve(
                        notification_id,
                        PauseResolution(
                            decision=decision,
                            data=_resolution_waiter_data(row.resolution, resolution_data),
                        ),
                    )
                    return True
                if row.status != "resolving":
                    return False
                existing = row.resolution if isinstance(row.resolution, dict) else {}
                if not _resolution_claim_matches(existing, decision, resolution_data):
                    return False
                claimed_at = row.resolved_at
                if claimed_at is not None and claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
                claim_is_stale = (
                    claimed_at is not None
                    and (datetime.now(UTC) - claimed_at).total_seconds()
                    >= _RESOLUTION_CLAIM_SECONDS
                )

            if claim_is_stale:
                return await self.resolve(
                    notification_id,
                    decision,
                    resolution_data,
                    user_email=user_email,
                    admission_guard=admission_guard,
                    selection_guard=selection_guard,
                    pre_submit_guard=pre_submit_guard,
                    human_answer=human_answer,
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_RESOLUTION_POLL_SECONDS, remaining))

    async def find_oldest_pending_escalation(
        self,
        *,
        user_email: str,
        conversation_id: str,
    ) -> Notification | None:
        """Return the oldest actionable escalation for one owned conversation."""
        rows = await self.list_actionable_escalations(
            user_email=user_email, conversation_id=conversation_id
        )
        return rows[0] if rows else None

    async def _actionable_escalation_rows(
        self, db: AsyncSession, *, user_email: str, conversation_id: str
    ) -> list[NotificationRow]:
        rows = (
            (
                await db.execute(
                    select(NotificationRow)
                    .where(
                        NotificationRow.user_email == user_email,
                        NotificationRow.notification_type == NotificationType.ESCALATION,
                        NotificationRow.status.in_(("pending", "resolving")),
                        or_(
                            NotificationRow.conversation_id == conversation_id,
                            NotificationRow.payload["managed_origin_conversation_id"].as_string()
                            == conversation_id,
                        ),
                    )
                    .order_by(NotificationRow.created_at, NotificationRow.notification_id)
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        return [
            row
            for row in rows
            if _notification_visible_in_conversation(row, conversation_id)
            and not _is_expired_notification(row, now=now)
        ]

    async def list_actionable_escalations(
        self, *, user_email: str, conversation_id: str
    ) -> list[Notification]:
        """List owned, visible, unexpired approvals; never pick one implicitly."""
        async with self._session_factory() as db:
            rows = await self._actionable_escalation_rows(
                db, user_email=user_email, conversation_id=conversation_id
            )
            return [_row_to_notification(row) for row in rows]

    async def find_escalation(
        self, *, notification_id: str, user_email: str, conversation_id: str
    ) -> Notification | None:
        """Find one owned visible approval, including an idempotent resolved retry."""
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, notification_id)
            if (
                row is None
                or row.user_email != user_email
                or row.notification_type != NotificationType.ESCALATION
                or not _notification_visible_in_conversation(row, conversation_id)
                or row.status not in {"pending", "resolving", "resolved"}
                or (
                    _is_expired_notification(row, now=datetime.now(UTC))
                    and (row.status == "pending" or not _has_live_resolution_claim(row))
                )
            ):
                return None
            return _row_to_notification(row)

    async def resolve_escalation(
        self,
        notification_id: str,
        decision: str,
        data: dict[str, Any],
        *,
        user_email: str,
        conversation_id: str,
        require_unique: bool = False,
        pre_submit_guard: Callable[[], bool] | None = None,
    ) -> bool:
        """Recheck selection at the resolution claim; keep resolution/CAS one-owned."""

        async def guard(db: AsyncSession) -> bool:
            row = await db.get(NotificationRow, notification_id)
            if (
                row is None
                or row.user_email != user_email
                or row.notification_type != NotificationType.ESCALATION
                or not _notification_visible_in_conversation(row, conversation_id)
            ):
                return False
            if row.status == "resolved":
                return True  # resolve() still validates identical decision and note.
            if require_unique and row.status == "pending":
                rows = await self._actionable_escalation_rows(
                    db, user_email=user_email, conversation_id=conversation_id
                )
                return len(rows) == 1 and rows[0].notification_id == notification_id
            return True

        selection_guard = None
        if require_unique:
            # Recheck ambiguity in the atomic claim, not just a prior read.
            other = aliased(NotificationRow)
            selection_guard = (
                ~select(other.notification_id)
                .where(
                    other.notification_id != notification_id,
                    other.user_email == user_email,
                    other.notification_type == NotificationType.ESCALATION,
                    other.status.in_(("pending", "resolving")),
                    or_(
                        other.conversation_id == conversation_id,
                        other.payload["managed_origin_conversation_id"].as_string()
                        == conversation_id,
                    ),
                    or_(other.expires_at.is_(None), other.expires_at > datetime.now(UTC)),
                )
                .exists()
            )
        return await self.resolve(
            notification_id,
            decision,
            data,
            user_email=user_email,
            admission_guard=guard,
            selection_guard=selection_guard,
            pre_submit_guard=pre_submit_guard,
        )

    async def find_tool_escalation(
        self,
        *,
        session_id: str,
        tool_call_id: str,
    ) -> Notification | None:
        """Find the durable escalation that owns an unresolved tool call."""
        async with self._session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(NotificationRow)
                        .where(
                            NotificationRow.notification_type == NotificationType.ESCALATION,
                            NotificationRow.session_id == session_id,
                            NotificationRow.status.in_(("pending", "resolving", "resolved")),
                        )
                        .order_by(NotificationRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else {}
                if payload.get("tool_call_id") == tool_call_id:
                    return _row_to_notification(row)
        return None

    async def find_tool_question(
        self,
        *,
        session_id: str,
        tool_call_id: str,
    ) -> Notification | None:
        """Find the durable question or auth challenge for one tool call."""

        async with self._session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(NotificationRow)
                        .where(
                            NotificationRow.notification_type.in_(
                                (
                                    NotificationType.STEP_QUESTION,
                                    AGENT_QUESTION,
                                    NotificationType.AUTH_CHALLENGE,
                                )
                            ),
                            NotificationRow.session_id == session_id,
                            NotificationRow.status.in_(("pending", "resolving", "resolved")),
                        )
                        .order_by(NotificationRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else {}
                if (
                    payload.get("tool_call_id") == tool_call_id
                    or payload.get("origin_call_id") == tool_call_id
                ):
                    return _row_to_notification(row)
        return None

    async def wait_for_resolution(
        self,
        notification_id: str,
        *,
        timeout: float,
        cancel_event: asyncio.Event | None = None,
        poll_seconds: float = _WAIT_RECONCILE_SECONDS,
    ) -> PauseResolution:
        """Wait on the local fast path while polling the authoritative DB row."""
        cancel_event = cancel_event or current_task_cancel_event()
        execution_fence = current_task_execution_fence()
        if execution_fence is not None:
            await execution_fence.suspend_capacity()
            from cognis.core.queue_wakeup import publish_queue_wakeup

            self._run_best_effort(
                publish_queue_wakeup(self._event_bus, self.cluster_signals),
                operation="queue_wakeup",
                notification_id=notification_id,
                timeout=_CLUSTER_PUBLISH_TIMEOUT_SECONDS,
            )
        deadline = monotonic() + timeout if timeout > 0 else None
        local_timeout = (
            timeout + _RESOLUTION_CLAIM_SECONDS + _RESOLUTION_COMPLETION_GRACE_SECONDS
            if timeout > 0
            else timeout
        )
        local_wait = asyncio.create_task(
            self._pause_waiter.wait(notification_id, timeout=local_timeout)
        )
        changed = asyncio.Event()

        async def on_change(event: Event) -> None:
            scope = event.data.get("scope") or {}
            if (
                event.data.get("kind") == "notification_state_changed"
                and scope.get("notification_id") == notification_id
            ):
                changed.set()

        # Register before the first durable read; a signal during that read stays set.
        self._event_bus.subscribe(EventType.CLUSTER_SCOPE_INVALIDATED, on_change)
        resolved: PauseResolution | None = None
        claim_grace_applied = False
        try:
            while True:
                changed.clear()
                recover_gate_claim: tuple[str, dict[str, Any]] | None = None
                if local_wait.done() and resolved is None:
                    with contextlib.suppress(TimeoutError):
                        # Local completion is a wakeup, not authority for a decision.
                        await local_wait
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError

                NOTIFICATION_WAIT_READS.inc()
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
                        if (
                            row.notification_type == NotificationType.GATE
                            and row.status == "resolving"
                            and isinstance(row.resolution, dict)
                            and row.resolved_at is not None
                        ):
                            gate_claimed_at = row.resolved_at.replace(
                                tzinfo=row.resolved_at.tzinfo or UTC
                            )
                            if (
                                datetime.now(UTC) - gate_claimed_at
                            ).total_seconds() >= _RESOLUTION_CLAIM_SECONDS:
                                recover_gate_claim = (
                                    str(row.resolution["decision"]),
                                    {
                                        key: value
                                        for key, value in row.resolution.items()
                                        if key not in {"decision", "state", "claim_token"}
                                    },
                                )
                        elif (
                            row.status == "resolving"
                            and deadline is not None
                            and monotonic() >= deadline
                            and not claim_grace_applied
                        ):
                            claimed_at = row.resolved_at
                            if claimed_at is not None and claimed_at.tzinfo is None:
                                claimed_at = claimed_at.replace(tzinfo=UTC)
                            if claimed_at is not None:
                                claim_remaining = max(
                                    0.0,
                                    _RESOLUTION_CLAIM_SECONDS
                                    - (datetime.now(UTC) - claimed_at).total_seconds(),
                                )
                                deadline = monotonic() + min(
                                    claim_remaining + _RESOLUTION_COMPLETION_GRACE_SECONDS,
                                    _RESOLUTION_CLAIM_SECONDS
                                    + _RESOLUTION_COMPLETION_GRACE_SECONDS,
                                )
                                claim_grace_applied = True

                if recover_gate_claim is not None:
                    recovered = await self.resolve(
                        notification_id, recover_gate_claim[0], recover_gate_claim[1]
                    )
                    if recovered:
                        continue  # Re-read the finalized durable winner.
                if resolved is None and deadline is not None and monotonic() >= deadline:
                    raise TimeoutError

                if resolved is not None and (
                    execution_fence is None or await execution_fence.ensure_capacity()
                ):
                    return resolved

                # Capacity retries retain their original cadence after resolution.
                delay = min(poll_seconds, _RESOLUTION_POLL_SECONDS) if resolved else poll_seconds
                if deadline is not None and resolved is None:
                    delay = min(delay, max(0.0, deadline - monotonic()))
                change_wait = asyncio.create_task(changed.wait())
                cancel_wait = (
                    asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
                )
                waits: set[asyncio.Task[Any]] = {change_wait}
                if cancel_wait is not None:
                    waits.add(cancel_wait)
                if not local_wait.done():
                    waits.add(local_wait)
                try:
                    await asyncio.wait(waits, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    transient = [change_wait] + ([cancel_wait] if cancel_wait is not None else [])
                    for pending in transient:
                        pending.cancel()
                    await asyncio.gather(*transient, return_exceptions=True)
        finally:
            self._event_bus.unsubscribe(EventType.CLUSTER_SCOPE_INVALIDATED, on_change)
            if not local_wait.done():
                local_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await local_wait

    async def _record_question_answer(
        self,
        *,
        notification_id: str,
        notification_payload: dict[str, Any],
        resolution_data: dict[str, Any],
        session_id: str | None,
        user_email: str,
    ) -> None:
        """Publish accepted human input before making the resolution observable."""
        if not session_id:
            raise ValueError("Question answer has no originating session")
        questions = notification_payload.get("questions") or []
        reply = validate_reply_for_questions(resolution_data, questions)
        by_id = {str(question["id"]): question for question in questions}
        parts = []
        for number, answer in enumerate(reply["answers"], 1):
            question = by_id.get(answer["question_id"], {})
            options = {str(option["id"]): option["label"] for option in question.get("options", [])}
            values = [options[key] for key in answer["selected_option_ids"]]
            if answer.get("custom_answer"):
                values.append(answer["custom_answer"])
            prefix = f"Answer {number}: " if len(reply["answers"]) > 1 else ""
            parts.append(prefix + "; ".join(values))
        content = "\n".join(parts)
        async with self._session_factory() as db:
            row = await get_session_row(db, session_id)
        if row is None or row.user_email != user_email:
            raise ValueError("Question session ownership could not be verified")
        intaris_id = row.intaris_session_id or session_id
        events = [
            SessionEvent(
                type="assistant_message",
                data={
                    "role": "assistant",
                    "content": json.dumps(questions, ensure_ascii=False),
                    "source": "question_context",
                    "question_id": notification_id,
                },
            ),
            SessionEvent(
                type="user_message",
                data={
                    "role": "user",
                    "content": content,
                    "content_type": "text",
                    "source": "question_answer",
                    "intention_eligible": True,
                    "question_id": notification_id,
                },
            ),
        ]
        with scoped_runtime_context(user_email=user_email):
            result = await self._providers.guardrails.record_events(
                session_id=intaris_id,
                events=events,
                source="cognis",
                idempotency_key=f"{intaris_id}:question_answer:{notification_id}",
                user_email=user_email,
            )
            if not result.ok:
                raise RuntimeError("Intaris did not persist the human question answer")
            # Reuse the normal event-to-intention path, including its evaluation barrier.
            await self._providers.guardrails.report_reasoning(
                session_id=intaris_id,
                from_events=True,
                wait_for_intention=True,
                wait_timeout_ms=10000,
            )

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
        if notification_type == AGENT_QUESTION:
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
                "origin_call_id": notification_payload.get("tool_call_id")
                or notification_payload.get("origin_call_id"),
                "origin_tool_name": notification_payload.get("tool_name")
                or notification_payload.get("origin_tool_name"),
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
                NotificationRow.status.in_(("pending", "resolving")),
                NotificationRow.notification_type != AGENT_QUESTION,
            )
            if task_id:
                stmt = stmt.where(NotificationRow.task_id == task_id)
            if session_id:
                stmt = stmt.where(NotificationRow.session_id == session_id)
            stmt = stmt.order_by(NotificationRow.created_at.desc())
            result = await db.execute(stmt)
            rows = result.scalars().all()
            if conversation_id:
                rows = [
                    row
                    for row in rows
                    if _notification_visible_in_conversation(row, conversation_id)
                ]

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
                if _is_expired_notification(row, now=now) and not _has_fresh_resolution_claim(
                    row, now=now
                ):
                    reason = (
                        "timeout"
                        if row.notification_type == NotificationType.ESCALATION
                        else "expired"
                    )
                    stale_notifications.append((row.notification_id, reason))
                    continue
                if _is_callback_only_notification(row.notification_type, row.payload):
                    visible_rows.append(_row_to_notification(row))
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
            if reason == "timeout":
                await self.resolve_timeout(notification_id)
            else:
                await self.mark_orphaned(
                    notification_id,
                    reason=reason,
                    resolving_before=now - timedelta(seconds=_RESOLUTION_CLAIM_SECONDS),
                )

        return visible_rows

    async def _reconcile_remote_escalation(self, row: NotificationRow) -> bool:
        """Resolve locally when Intaris already recorded an external decision."""
        if row.notification_type != NotificationType.ESCALATION or row.status not in {
            "pending",
            "resolving",
        }:
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
                    NotificationRow.status == row.status,
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
                    revision=NotificationRow.revision + 1,
                )
            )
            await db.commit()
            rowcount = getattr(result, "rowcount", None)
            if rowcount is not None and not int(rowcount or 0):
                current = await db.get(NotificationRow, row.notification_id)
                return bool(
                    current is not None and _is_same_resolution(current, decision, {"note": note})
                )

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
                    "resumes_execution": True,
                    "managed_origin_conversation_id": _managed_origin_conversation_id(
                        row.payload if isinstance(row.payload, dict) else None,
                        target_conversation_id=row.conversation_id,
                    ),
                },
            )
        )
        self._run_best_effort(
            self._publish_notification_cluster_changes(
                conversation_id=row.conversation_id,
                user_email=row.user_email,
                notification_id=row.notification_id,
                task_id=row.task_id,
                session_id=row.session_id,
                revision=now,
                payload=row.payload if isinstance(row.payload, dict) else None,
            ),
            operation="cluster_publish",
            notification_id=row.notification_id,
            timeout=_CLUSTER_PUBLISH_TIMEOUT_SECONDS,
        )
        self._run_best_effort(
            self._record_user_interaction(
                notification_id=row.notification_id,
                notification_type=str(row.notification_type),
                notification_payload=dict(row.payload) if isinstance(row.payload, dict) else {},
                resolution_data={"note": note},
                decision=decision,
                session_id=row.session_id,
                user_email=row.user_email,
            ),
            operation="interaction_record",
            notification_id=row.notification_id,
            timeout=_INTERACTION_RECORD_TIMEOUT_SECONDS,
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

    async def resolve_timeout(self, notification_id: str) -> PauseResolution | None:
        """Atomically deny a pending or stale resolving escalation on timeout."""
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=_RESOLUTION_CLAIM_SECONDS)
        resolution = {"decision": "deny", "reason": "timeout", "state": "timed_out"}
        async with self._session_factory() as db:
            result = await db.execute(
                update(NotificationRow)
                .where(
                    NotificationRow.notification_id == notification_id,
                    or_(
                        NotificationRow.status == "pending",
                        and_(
                            NotificationRow.status == "resolving",
                            NotificationRow.resolved_at <= stale_before,
                        ),
                    ),
                )
                .values(
                    status="resolved",
                    resolution=resolution,
                    resolved_at=now,
                    revision=NotificationRow.revision + 1,
                )
            )
            await db.commit()
            if not int(getattr(result, "rowcount", 0) or 0):
                row = await db.get(NotificationRow, notification_id)
                if (
                    row is not None
                    and row.status == "resolved"
                    and isinstance(row.resolution, dict)
                ):
                    return PauseResolution(
                        decision=str(row.resolution.get("decision") or "deny"),
                        data=_resolution_waiter_data(row.resolution, {}),
                    )
                return None
            row = await db.get(NotificationRow, notification_id)
        if row is None:
            return None
        terminal = PauseResolution(decision="deny", data={"reason": "timeout"})
        self._pause_waiter.resolve(notification_id, terminal)
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
                    "decision": "deny",
                    "managed_origin_conversation_id": _managed_origin_conversation_id(
                        row.payload if isinstance(row.payload, dict) else None,
                        target_conversation_id=row.conversation_id,
                    ),
                },
            )
        )
        self._run_best_effort(
            self._publish_notification_cluster_changes(
                conversation_id=row.conversation_id,
                user_email=row.user_email,
                notification_id=row.notification_id,
                task_id=row.task_id,
                session_id=row.session_id,
                revision=now,
                payload=row.payload if isinstance(row.payload, dict) else None,
            ),
            operation="cluster_publish",
            notification_id=row.notification_id,
            timeout=_CLUSTER_PUBLISH_TIMEOUT_SECONDS,
        )
        return terminal

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

    async def mark_orphaned(
        self,
        notification_id: str,
        *,
        reason: str,
        resolving_before: datetime | None = None,
        include_resolving: bool = False,
    ) -> bool:
        """Mark a stale notification as terminal without resuming a waiter."""
        # Expiry is a configured workflow decision, not blanket cancellation.
        # Legacy records without a policy are reconciled by the workflow engine.
        if reason == "expired":
            notification = await self.get(notification_id)
            if notification is not None and notification.notification_type == NotificationType.GATE:
                action = notification.payload.get("timeout_action")
                if action not in {"fail", "continue", "cancel"}:
                    return False
                return await self.resolve(notification_id, action, {"reason": "timeout"})
        now = datetime.now(UTC)
        terminal_predicate = NotificationRow.status == "pending"
        if include_resolving:
            # Only the owning cancelled/expired wait may fence a live claim.
            terminal_predicate = NotificationRow.status.in_(("pending", "resolving"))
        elif resolving_before is not None:
            stale_resolving = and_(
                NotificationRow.status == "resolving",
                or_(
                    NotificationRow.resolved_at.is_(None),
                    NotificationRow.resolved_at <= resolving_before,
                ),
            )
            terminal_predicate = or_(terminal_predicate, stale_resolving)
        async with self._session_factory() as db:
            row = await db.get(NotificationRow, notification_id)
            if row is None:
                return False
            result = await db.execute(
                update(NotificationRow)
                .where(
                    NotificationRow.notification_id == notification_id,
                    terminal_predicate,
                )
                .execution_options(synchronize_session=False)
                .values(
                    status="resolved",
                    resolution={"decision": "cancel", "reason": reason},
                    resolved_at=now,
                    revision=NotificationRow.revision + 1,
                )
            )
            await db.commit()
            if not int(getattr(result, "rowcount", 0) or 0):
                return False
        self._pause_waiter.clear(notification_id)
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
                    "managed_origin_conversation_id": _managed_origin_conversation_id(
                        row.payload if isinstance(row.payload, dict) else None,
                        target_conversation_id=row.conversation_id,
                    ),
                },
            )
        )
        self._run_best_effort(
            self._publish_notification_cluster_changes(
                conversation_id=row.conversation_id,
                user_email=row.user_email,
                notification_id=row.notification_id,
                task_id=row.task_id,
                session_id=row.session_id,
                revision=now,
                payload=row.payload if isinstance(row.payload, dict) else None,
            ),
            operation="cluster_publish",
            notification_id=row.notification_id,
            timeout=_CLUSTER_PUBLISH_TIMEOUT_SECONDS,
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
                select(NotificationRow).where(
                    NotificationRow.task_id == task_id,
                    NotificationRow.status == "pending",
                )
            )
            rows = list(result.scalars().all())

        resolved = 0
        for row in rows:
            if _is_callback_only_notification(row.notification_type, row.payload):
                self._pause_waiter.clear(row.notification_id)
                continue
            if await self.mark_orphaned(row.notification_id, reason=reason):
                resolved += 1
        return resolved

    # ------------------------------------------------------------------
    # Local pause reconciliation
    # ------------------------------------------------------------------

    async def reconcile_escalation_pauses(self, *, conversation_id: str, user_email: str) -> bool:
        """Discard only terminal, matching orphan pauses before a queue claim.

        This reconciles local metadata, not decisions: live waiters retain their
        durable resolution path, and no resolution or receipt is delivered here.
        """
        pauses = self._pause_waiter.list_pending(
            conversation_id=conversation_id, pause_type=NotificationType.ESCALATION
        )
        if not pauses:
            return True
        try:
            async with asyncio.timeout(_PAUSE_RECONCILE_TIMEOUT_SECONDS):
                async with self._session_factory() as db:
                    result = await db.execute(
                        select(NotificationRow).where(
                            NotificationRow.notification_id.in_(
                                [pause.pause_id for pause in pauses]
                            ),
                            NotificationRow.user_email == user_email,
                            NotificationRow.conversation_id == conversation_id,
                            NotificationRow.notification_type == NotificationType.ESCALATION,
                        )
                    )
                    rows = {row.notification_id: row for row in result.scalars().all()}
        except Exception:
            logger.warning(
                "notification: pre-claim pause reconciliation failed",
                extra={"extra_data": {"conversation_id": conversation_id}},
                exc_info=True,
            )
            return False
        # There is no await between validating the current pause and discarding it.
        for pause in pauses:
            row = rows.get(pause.pause_id)
            if (
                row is not None
                and row.status == "resolved"
                and isinstance(row.resolution, dict)
                and row.resolution.get("decision") in ("approve", "deny", "cancel")
                and row.session_id == pause.session_id
                and row.task_id == pause.task_id
                and row.step_name == pause.step_name
                and row.step_run_id == pause.step_run_id
                and self._pause_waiter.get(pause.pause_id) is pause
            ):
                self._pause_waiter.discard_unwatched(pause.pause_id)
        return (
            self._pause_waiter.find_pending(
                conversation_id=conversation_id, pause_type=NotificationType.ESCALATION
            )
            is None
        )

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
            stmt = select(NotificationRow).where(
                NotificationRow.status.in_(("pending", "resolving"))
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()
            task_status_cache: dict[str, str | None] = {}
            direct_turns_by_session: dict[str, list[DirectTurnRequestRow]] = {}
            for row in rows:
                if row.task_id and row.task_id not in task_status_cache:
                    task_row = await get_task(db, row.task_id)
                    task_status_cache[row.task_id] = (
                        str(task_row.status) if task_row is not None else None
                    )
                if (
                    row.task_id is None
                    and row.notification_type
                    in {NotificationType.STEP_QUESTION, NotificationType.AUTH_CHALLENGE}
                    and row.session_id
                    and row.session_id not in direct_turns_by_session
                ):
                    direct_turns_by_session[row.session_id] = list(
                        (
                            await db.execute(
                                select(DirectTurnRequestRow)
                                .where(DirectTurnRequestRow.session_id == row.session_id)
                                .order_by(DirectTurnRequestRow.admission_order.desc())
                            )
                        )
                        .scalars()
                        .all()
                    )

        count = 0
        orphaned_count = 0
        for row in rows:
            if _is_expired_notification(row, now=datetime.now(UTC)):
                if row.notification_type == NotificationType.ESCALATION:
                    await self.resolve_timeout(row.notification_id)
                else:
                    await self.mark_orphaned(
                        row.notification_id,
                        reason="expired",
                        resolving_before=datetime.now(UTC)
                        - timedelta(seconds=_RESOLUTION_CLAIM_SECONDS),
                    )
                orphaned_count += 1
                continue
            if _is_callback_only_notification(row.notification_type, row.payload):
                self._pause_waiter.clear(row.notification_id)
                continue
            if (
                row.notification_type
                in {NotificationType.STEP_QUESTION, NotificationType.AUTH_CHALLENGE}
                and row.task_id is None
            ):
                owner = _find_direct_turn_owner(
                    row,
                    direct_turns_by_session.get(row.session_id or "", []),
                )
                if owner is not None and owner.status in {
                    "completed",
                    "failed",
                    "cancelled",
                    "absorbed",
                    "ambiguous",
                }:
                    if await self.mark_orphaned(
                        row.notification_id,
                        reason="direct_turn_terminal",
                    ):
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
                "notification: marked %d direct-chat questions orphaned after restart",
                orphaned_count,
            )
        return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_callback_only_notification(
    notification_type: str,
    payload: dict[str, Any] | None,
) -> bool:
    if notification_type != NotificationType.AUTH_CHALLENGE or not isinstance(payload, dict):
        return False
    metadata = payload.get("metadata")
    return (
        payload.get("kind") == "oauth_authorization"
        and isinstance(metadata, dict)
        and metadata.get("callback_only") is True
    )


def _find_direct_turn_owner(
    notification: NotificationRow,
    candidates: list[DirectTurnRequestRow],
) -> DirectTurnRequestRow | None:
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    call_id = payload.get("tool_call_id") or payload.get("origin_call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    for candidate in candidates:
        outcome = candidate.outcome if isinstance(candidate.outcome, dict) else {}
        descriptors = outcome.get("tool_calls")
        if not isinstance(descriptors, list):
            continue
        if any(
            isinstance(descriptor, dict) and descriptor.get("call_id") == call_id
            for descriptor in descriptors
        ):
            return candidate
    return None


def _row_to_notification(row: NotificationRow) -> Notification:
    payload = row.payload or {}
    if (
        row.notification_type == NotificationType.GATE
        and row.task_id
        and isinstance(payload.get("options"), list)
    ):
        payload = {**payload, "options": effective_gate_options(payload.get("options"))}
    return Notification(
        notification_id=row.notification_id,
        notification_type=row.notification_type,
        user_email=row.user_email,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        step_name=row.step_name,
        step_run_id=row.step_run_id,
        session_id=row.session_id,
        payload=payload,
        status=row.status,
        resolution=row.resolution,
        revision=_notification_revision(row),
        expires_at=getattr(row, "expires_at", None),
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
        from cognis.providers.guardrails.resolution_errors import stale_approval_result

        if stale_approval_result(decision, resolution_data) is not None:
            return {
                "title": "Approval expired",
                "summary": "Session or policy changed; re-evaluation is required. No user denial.",
                "answers": [],
                "status": "cancelled",
            }
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
        from cognis import json_codec as json

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
    submission_id = resolution_data.get("submission_id")
    if submission_id is not None:
        return bool(row.resolution.get("submission_id") == submission_id)
    if row.resolution.get("state") == "resolved_remote":
        return True
    persisted_data = {
        key: value for key, value in row.resolution.items() if key not in {"decision", "state"}
    }
    return _normalize_resolution_data(persisted_data) == _normalize_resolution_data(resolution_data)


def _question_resolution_matches(resolution: Any, decision: str, data: dict[str, Any]) -> bool:
    if not isinstance(resolution, dict) or resolution.get("decision") != decision:
        return False
    persisted = {
        k: v for k, v in resolution.items() if k not in {"decision", "state", "claim_token"}
    }
    return _normalize_resolution_data(persisted) == _normalize_resolution_data(data)


def _resolution_claim_matches(
    resolution: dict[str, Any],
    decision: str,
    resolution_data: dict[str, Any],
    *,
    require_exact_payload: bool = False,
) -> bool:
    """Return true when an in-flight claim represents the same request."""

    if str(resolution.get("decision") or "").lower() != decision.lower():
        return False
    submission_id = resolution_data.get("submission_id")
    if submission_id is not None and not require_exact_payload:
        return bool(resolution.get("submission_id") == submission_id)
    persisted_data = {
        key: value
        for key, value in resolution.items()
        if key not in {"decision", "state", "claim_token"}
    }
    return _normalize_resolution_data(persisted_data) == _normalize_resolution_data(resolution_data)


def _claim_update_succeeded(result: Any, *, row: Any, claim_token: str) -> bool:
    """Read a real SQL rowcount while supporting lightweight unit DB stubs."""
    rowcount = getattr(result, "rowcount", None)
    if rowcount is not None:
        return bool(int(rowcount or 0))
    resolution = row.resolution if isinstance(row.resolution, dict) else {}
    return row.status == "resolving" and resolution.get("claim_token") == claim_token


def _normalize_resolution_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize semantically equivalent persisted/input resolution payloads."""
    normalized = dict(data)
    if normalized.get("note") == "":
        normalized.pop("note")
    return normalized


def _notification_revision(row: Any) -> int:
    return int(getattr(row, "revision", 1) or 1)


def _notification_payload_expires_at(
    notification_type: str,
    payload: dict[str, Any],
    *,
    created_at: datetime,
) -> datetime | None:
    from cognis.store.notification_expiry import notification_payload_expiry

    return notification_payload_expiry(notification_type, payload, created_at=created_at)


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


def _notification_expiry(row: NotificationRow) -> datetime | None:
    """Resolve explicit or legacy payload-derived expiry at the canonical owner."""
    from cognis.store.notification_expiry import notification_expiry

    return notification_expiry(row)


def _is_expired_notification(row: NotificationRow, *, now: datetime) -> bool:
    """Return true when an actionable notification has exceeded its expiry."""
    expiry = _notification_expiry(row)
    return row.status in {"pending", "resolving"} and expiry is not None and now >= expiry


def _unexpired_notification_predicate(
    row: NotificationRow, db: AsyncSession
) -> ColumnElement[bool]:
    """Apply the same explicit/legacy deadline inside the atomic claim."""
    from cognis.store.coordination import database_now_expression

    expiry = func.coalesce(NotificationRow.expires_at, _notification_expiry(row))
    return or_(expiry.is_(None), expiry > database_now_expression(db))


def _has_live_resolution_claim(row: NotificationRow) -> bool:
    claimed_at = row.resolved_at
    if claimed_at is None:
        return False
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - claimed_at).total_seconds() < _RESOLUTION_CLAIM_SECONDS


def _has_fresh_resolution_claim(row: NotificationRow, *, now: datetime) -> bool:
    if row.status != "resolving" or row.resolved_at is None:
        return False
    claimed_at = row.resolved_at
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=UTC)
    return (now - claimed_at).total_seconds() < _RESOLUTION_CLAIM_SECONDS
