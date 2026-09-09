"""Unified notification endpoints for escalations, gates, and step questions."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cognis.api.attention_actions import (
    attention_action_expires_at,
    get_attention_row,
    project_attention_detail,
)
from cognis.api.common import forbid_mutation_for_viewer, require_current_user
from cognis.api.models import (
    AttentionActionDetail,
    CredentialUpsertRequest,
    QuestionSetAnswer,
)
from cognis.core.credential_grants import grant_credential_to_agent
from cognis.core.management import resolve_task_pause_action, respond_task_input
from cognis.core.notification_resolution import (
    build_auth_challenge_resolution_data,
    build_credential_request_resolution_data,
)
from cognis.core.notifications import Notification, NotificationService
from cognis.core.question_sets import plain_text_reply_for_questions, validate_reply_for_questions

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
attention_router = APIRouter(prefix="/api/v1/attention-actions", tags=["attention-actions"])


class NotificationResponse(BaseModel):
    """API response for a notification."""

    notification_id: str
    notification_type: str
    conversation_id: str
    task_id: str | None = None
    step_name: str | None = None
    step_run_id: str | None = None
    session_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    status: str = "pending"
    resolution: dict[str, object] | None = None
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


class AttentionActionResolveRequest(BaseModel):
    """Revision-bound input for one focused dashboard action."""

    expected_revision: int = Field(ge=1)
    submission_id: str = Field(min_length=8, max_length=128)
    action: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    feedback: str | None = Field(default=None, max_length=4_000)
    answers: list[QuestionSetAnswer] | None = None
    response_fields: dict[str, str] | None = None
    credential: CredentialUpsertRequest | None = None


class AttentionActionResolveResponse(BaseModel):
    action_id: str
    status: str
    decision: str | None = None
    revision: int
    convergence_id: str


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
    For step questions: ``decision`` is ``continue`` or ``cancel``, ``response`` is the answer.
    """
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    svc = _get_service(request)

    # Verify ownership before resolving
    notification = await svc.get(notification_id)
    if notification is None or notification.user_email != user.email:
        raise HTTPException(status_code=404, detail="Notification not found")
    if (
        notification.notification_type == "auth_challenge"
        and isinstance(notification.payload, dict)
        and notification.payload.get("kind") == "oauth_authorization"
        and payload.decision not in {"cancel", "deny"}
    ):
        raise HTTPException(
            status_code=400,
            detail="OAuth authorization challenges are completed by the provider authorization flow",
        )

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
    if notification.notification_type == "step_question" and payload.decision not in {
        "cancel",
        "deny",
    }:
        raw_questions = (
            (notification.payload or {}).get("questions")
            if isinstance(notification.payload, dict)
            else []
        )
        questions = (
            [item for item in raw_questions if isinstance(item, dict)]
            if isinstance(raw_questions, list)
            else []
        )
        try:
            if payload.response_payload is not None:
                data = validate_reply_for_questions(dict(payload.response_payload), questions)
                answers = data.get("answers")
                if data.get("mode") == "plain_text" and not (
                    isinstance(answers, list)
                    and any(
                        str(answer.get("custom_answer") or "").strip()
                        for answer in answers
                        if isinstance(answer, dict)
                    )
                ):
                    raise ValueError("Question response is required")
            elif isinstance(payload.response, str) and payload.response.strip():
                data = plain_text_reply_for_questions(payload.response, questions)
            else:
                raise ValueError("Question response is required")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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


def _terminal_action_response(notification: Notification) -> AttentionActionResolveResponse:
    resolution = notification.resolution if isinstance(notification.resolution, dict) else {}
    return AttentionActionResolveResponse(
        action_id=notification.notification_id,
        status=notification.status,
        decision=(
            str(resolution.get("decision")) if resolution.get("decision") is not None else None
        ),
        revision=notification.revision,
        convergence_id=f"{notification.notification_id}:{notification.revision}",
    )


async def _owned_attention_notification(request: Request, action_id: str) -> Notification:
    user = require_current_user(request)
    notification = await _get_service(request).get(action_id)
    if notification is None or notification.user_email != user.email:
        raise HTTPException(status_code=404, detail="Attention action not found")
    return notification


@attention_router.get("/{action_id}", response_model=AttentionActionDetail)
async def get_attention_action(request: Request, action_id: str) -> AttentionActionDetail:
    """Return an owner-authorized, allowlisted focused action detail."""

    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_attention_row(session, owner_email=user.email, action_id=action_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Attention action not found")
        return project_attention_detail(row, can_mutate=user.role != "viewer")


@attention_router.post(
    "/{action_id}/resolve",
    response_model=AttentionActionResolveResponse,
)
async def resolve_attention_action(
    request: Request,
    action_id: str,
    payload: AttentionActionResolveRequest,
) -> AttentionActionResolveResponse:
    """Resolve one owner action against its exact server revision."""

    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    service = _get_service(request)
    notification = await _owned_attention_notification(request, action_id)
    persisted_resolution = (
        notification.resolution if isinstance(notification.resolution, dict) else {}
    )
    if notification.status == "resolved":
        if (
            persisted_resolution.get("submission_id") == payload.submission_id
            and str(persisted_resolution.get("decision") or "") == payload.action
        ):
            return _terminal_action_response(notification)
        raise HTTPException(status_code=409, detail="Attention action is already resolved")

    async with request.app.state.session_factory() as session:
        row = await get_attention_row(session, owner_email=user.email, action_id=action_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Attention action not found")
        detail = project_attention_detail(row, can_mutate=True)
        expires_at = attention_action_expires_at(row)
    if detail.status == "expired":
        if notification.notification_type == "escalation":
            await service.resolve_timeout(action_id)
        else:
            await service.mark_orphaned(action_id, reason="timeout")
        raise HTTPException(status_code=409, detail="Attention action expired")

    same_inflight_submission = (
        notification.status == "resolving"
        and persisted_resolution.get("submission_id") == payload.submission_id
        and str(persisted_resolution.get("decision") or "") == payload.action
    )
    if not same_inflight_submission and notification.revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="Attention action revision changed")
    allowed = {choice.action for choice in detail.allowed_actions}
    if not same_inflight_submission and payload.action not in allowed:
        raise HTTPException(status_code=409, detail="Attention action is not currently resolvable")
    if expires_at is not None and datetime.now(UTC) >= expires_at:
        raise HTTPException(status_code=409, detail="Attention action expired")

    base_data: dict[str, object] = {"submission_id": payload.submission_id}
    if payload.note:
        base_data["note"] = payload.note
    if payload.feedback:
        base_data["feedback"] = payload.feedback

    if detail.kind in {"gate", "step_question"} and notification.task_id:
        from cognis.api.routes.tasks import _row_to_task
        from cognis.store.queries import get_task

        async with request.app.state.session_factory() as session:
            task_row = await get_task(session, notification.task_id)
        if task_row is None or task_row.created_by != user.email:
            raise HTTPException(status_code=404, detail="Task not found")
        task = _row_to_task(task_row)
        try:
            if detail.kind == "gate":
                await resolve_task_pause_action(
                    task=task,
                    requested_action=payload.action,
                    note=(payload.feedback or payload.note or "").strip(),
                    pause_waiter=request.app.state.pause_waiter,
                    notification_service=service,
                    task_queue=request.app.state.task_queue,
                    session_factory=request.app.state.session_factory,
                    user_email=user.email,
                    notification_id=action_id,
                    expected_revision=payload.expected_revision,
                    submission_id=payload.submission_id,
                )
            elif payload.action == "cancel":
                ok = await service.resolve(
                    action_id,
                    "cancel",
                    base_data,
                    user_email=user.email,
                    expected_revision=payload.expected_revision,
                )
                if not ok:
                    raise RuntimeError("Step question has already been resolved")
            else:
                if payload.answers is None:
                    raise HTTPException(status_code=400, detail="Question answers are required")
                await respond_task_input(
                    task=task,
                    reply={
                        "mode": "structured",
                        "answers": [answer.model_dump(mode="json") for answer in payload.answers],
                    },
                    pause_waiter=request.app.state.pause_waiter,
                    notification_service=service,
                    task_queue=request.app.state.task_queue,
                    session_factory=request.app.state.session_factory,
                    user_email=user.email,
                    notification_id=action_id,
                    expected_revision=payload.expected_revision,
                    submission_id=payload.submission_id,
                )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        data_factory = None
        response_fields: dict[str, object] | None = (
            dict(payload.response_fields) if payload.response_fields is not None else None
        )
        if detail.kind == "step_question" and payload.action != "cancel":
            if payload.answers is None:
                raise HTTPException(status_code=400, detail="Question answers are required")
            try:
                base_data.update(
                    validate_reply_for_questions(
                        {
                            "mode": "structured",
                            "answers": [
                                answer.model_dump(mode="json") for answer in payload.answers
                            ],
                        },
                        [question.model_dump(mode="json") for question in detail.display.questions],
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif detail.kind == "credential_request":

            async def build_credential() -> dict[str, object]:
                data = await build_credential_request_resolution_data(
                    notification=notification,
                    decision=payload.action,
                    user_email=user.email,
                    credentials_provider=request.app.state.providers.credentials,
                    response_payload=response_fields,
                    credential=payload.credential,
                    idempotency_key=f"{action_id}:{payload.submission_id}",
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
                return data

            data_factory = build_credential
        elif detail.kind == "auth_challenge":

            async def build_auth() -> dict[str, object]:
                return await build_auth_challenge_resolution_data(
                    notification=notification,
                    decision=payload.action,
                    user_email=user.email,
                    credentials_provider=request.app.state.providers.credentials,
                    response_payload=response_fields,
                    idempotency_key=f"{action_id}:{payload.submission_id}",
                )

            data_factory = build_auth

        try:
            ok = await service.resolve(
                action_id,
                payload.action,
                base_data,
                user_email=user.email,
                expected_revision=payload.expected_revision,
                data_factory=data_factory,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=409, detail="Attention action changed")

    terminal = await _owned_attention_notification(request, action_id)
    if terminal.status != "resolved":
        raise HTTPException(status_code=409, detail="Attention action is still resolving")
    return _terminal_action_response(terminal)
