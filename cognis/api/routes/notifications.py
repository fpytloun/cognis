"""Unified notification endpoints for escalations, gates, and step questions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cognis.api.common import require_current_user
from cognis.core.notifications import Notification, NotificationService

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class NotificationResponse(BaseModel):
    """API response for a notification."""

    notification_id: str
    notification_type: str
    conversation_id: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    payload: dict = {}
    status: str = "pending"
    resolution: dict | None = None
    created_at: str | None = None
    resolved_at: str | None = None


class ResolveRequest(BaseModel):
    """Request body for resolving any notification type."""

    decision: str
    note: str | None = None
    response: str | None = None
    feedback: str | None = None


def _to_response(n: Notification) -> NotificationResponse:
    return NotificationResponse(
        notification_id=n.notification_id,
        notification_type=n.notification_type,
        conversation_id=n.conversation_id,
        task_id=n.task_id,
        step_name=n.step_name,
        step_run_id=n.step_run_id,
        session_id=n.session_id,
        payload=n.payload,
        status=n.status,
        resolution=n.resolution,
        created_at=n.created_at.isoformat() if n.created_at else None,
        resolved_at=n.resolved_at.isoformat() if n.resolved_at else None,
    )


def _get_service(request: Request) -> NotificationService:
    svc: NotificationService | None = getattr(request.app.state, "notification_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Notification service not available")
    return svc


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    request: Request,
    conversation_id: str | None = None,
) -> list[NotificationResponse]:
    """List pending notifications for the current user."""
    user = require_current_user(request)
    svc = _get_service(request)
    notifications = await svc.list_pending(user.email, conversation_id=conversation_id)
    return [_to_response(n) for n in notifications]


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(
    request: Request,
    notification_id: str,
) -> NotificationResponse:
    """Get a single notification by ID."""
    user = require_current_user(request)
    svc = _get_service(request)
    notification = await svc.get(notification_id)
    if notification is None or notification.user_email != user.email:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _to_response(notification)


@router.post("/{notification_id}/resolve", response_model=dict)
async def resolve_notification(
    request: Request,
    notification_id: str,
    payload: ResolveRequest,
) -> dict[str, object]:
    """Resolve any notification type (escalation, gate, step question).

    For escalations: ``decision`` is ``approve`` or ``deny``, ``note`` is optional.
    For gates: ``decision`` is ``continue`` or ``cancel``, ``feedback`` is optional.
    For step questions: ``decision`` is ``continue``, ``response`` is the answer.
    """
    user = require_current_user(request)
    svc = _get_service(request)

    # Verify ownership before resolving
    notification = await svc.get(notification_id)
    if notification is None or notification.user_email != user.email:
        raise HTTPException(status_code=404, detail="Notification not found")

    if notification.notification_type == "step_question" and notification.task_id is None:
        pause = request.app.state.pause_waiter.get(notification_id)
        if pause is None or pause.pause_type != "step_question" or pause.task_id is not None:
            raise HTTPException(status_code=409, detail="Step question can no longer be resumed")

    # Build resolution data from the request
    data: dict[str, object] = {}
    if payload.note:
        data["note"] = payload.note
    if payload.response is not None:
        data["response"] = payload.response
    if payload.feedback:
        data["feedback"] = payload.feedback

    ok = await svc.resolve(notification_id, payload.decision, data, user_email=user.email)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Notification already resolved or not found",
        )
    return {"ok": True, "notification_id": notification_id, "decision": payload.decision}
