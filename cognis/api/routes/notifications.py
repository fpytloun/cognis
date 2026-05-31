"""Unified notification endpoints for escalations, gates, and step questions."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cognis.api.common import require_current_user
from cognis.api.models import CredentialUpsertRequest
from cognis.core.credential_grants import grant_credential_to_agent
from cognis.core.notification_resolution import (
    build_auth_challenge_resolution_data,
    build_credential_request_resolution_data,
)
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
    response_payload: dict[str, object] | None = None
    credential: CredentialUpsertRequest | None = None


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
    task_id: str | None = None,
    session_id: str | None = None,
) -> list[NotificationResponse]:
    """List pending notifications for the current user."""
    user = require_current_user(request)
    svc = _get_service(request)
    notifications = await svc.list_pending(
        user.email,
        conversation_id=conversation_id,
        task_id=task_id,
        session_id=session_id,
    )
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
    if (
        notification.notification_type == "auth_challenge"
        and isinstance(notification.payload, dict)
        and notification.payload.get("kind") == "oauth_authorization"
    ):
        raise HTTPException(
            status_code=400,
            detail="OAuth authorization challenges are completed by callback only",
        )

    if notification.notification_type == "step_question" and notification.task_id is None:
        pause = request.app.state.pause_waiter.get(notification_id)
        if pause is None or pause.pause_type != "step_question" or pause.task_id is not None:
            raise HTTPException(status_code=409, detail="Step question can no longer be resumed")

    if notification.notification_type == "credential_request":
        if payload.decision not in {"approve", "cancel", "deny"}:
            raise HTTPException(status_code=400, detail="Invalid credential request decision")
        if (
            payload.decision == "approve"
            and payload.credential is None
            and payload.response is None
            and payload.response_payload is None
        ):
            raise HTTPException(
                status_code=400,
                detail="Credential payload or response text required for approval",
            )
    if notification.notification_type == "auth_challenge" and payload.decision not in {
        "approve",
        "continue",
        "completed",
        "cancel",
        "deny",
    }:
        raise HTTPException(status_code=400, detail="Invalid auth challenge decision")

    # Build resolution data from the request
    data: dict[str, object] = {}
    if payload.note:
        data["note"] = payload.note
    if payload.response is not None:
        data["response"] = payload.response
    if payload.feedback:
        data["feedback"] = payload.feedback
    if payload.response_payload:
        data["response_payload"] = payload.response_payload

    if notification.notification_type == "credential_request" and payload.decision != "approve":
        data = {}
    elif notification.notification_type == "credential_request" and payload.decision == "approve":
        try:
            data = await build_credential_request_resolution_data(
                notification=notification,
                decision=payload.decision,
                user_email=user.email,
                credentials_provider=request.app.state.providers.credentials,
                response=payload.response,
                response_payload=payload.response_payload,
                credential=payload.credential,
            )
            requested = notification.payload if isinstance(notification.payload, dict) else {}
            agent_id = requested.get("agent_id")
            credential_id = data.get("credential_id")
            if isinstance(agent_id, str) and isinstance(credential_id, str):
                async with request.app.state.session_factory() as session:
                    granted = await grant_credential_to_agent(
                        session,
                        agent_id=agent_id,
                        credential_id=credential_id,
                        owner_email=user.email,
                    )
                    if granted:
                        await session.commit()
                    data["credential_granted_to_agent"] = True
                    data["agent_permissions_updated"] = granted
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif notification.notification_type == "auth_challenge":
        try:
            data = await build_auth_challenge_resolution_data(
                notification=notification,
                decision=payload.decision,
                user_email=user.email,
                credentials_provider=request.app.state.providers.credentials,
                response=payload.response,
                response_payload=payload.response_payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    ok = await svc.resolve(notification_id, payload.decision, data, user_email=user.email)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Notification already resolved or not found",
        )
    return {"ok": True, "notification_id": notification_id, "decision": payload.decision}
