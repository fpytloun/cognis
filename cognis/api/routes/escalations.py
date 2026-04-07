"""Escalation proxy routes.

These endpoints are kept for backward compatibility.  New clients should
use the unified ``/api/v1/notifications`` endpoints instead.
"""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, HTTPException, Request

from cognis.api.common import require_current_user
from cognis.api.models import EscalationResolveRequest, EscalationResponse
from cognis.api.serializers import escalation_to_response
from cognis.core.agent_loop import PauseResolution
from cognis.core.notifications import NotificationService

router = APIRouter(prefix="/api/v1/escalations", tags=["escalations"])


@router.get("", response_model=list[EscalationResponse])
async def list_escalations(
    request: Request, session_id: str | None = None
) -> list[EscalationResponse]:
    require_current_user(request)
    rows = await request.app.state.providers.guardrails.list_pending_escalations(
        session_id=session_id
    )
    return [escalation_to_response(row) for row in rows]


@router.post("/{call_id}/resolve", response_model=dict)
async def resolve_escalation(
    request: Request,
    call_id: str,
    payload: EscalationResolveRequest,
) -> dict[str, object]:
    user = require_current_user(request)

    # Try the unified notification service first (call_id is the notification_id
    # for escalation-type notifications).
    svc: NotificationService | None = getattr(request.app.state, "notification_service", None)
    if svc is not None:
        ok = await svc.resolve(
            call_id,
            payload.decision,
            {"note": payload.note or ""},
            user_email=user.email,
        )
        if ok:
            return {"ok": True, "call_id": call_id, "decision": payload.decision}
        raise HTTPException(
            status_code=409,
            detail="Escalation could not be resolved because the upstream approval state was not confirmed.",
        )

    # Fallback: resolve via PauseWaiter directly (legacy path for
    # escalations created before the notification service was available).
    pause_id = f"escalation:{call_id}"
    submitted = False
    with contextlib.suppress(Exception):
        await request.app.state.providers.guardrails.submit_decision(
            call_id,
            payload.decision,
            payload.note,
        )
        submitted = True
    if not submitted:
        raise HTTPException(
            status_code=502,
            detail="Unable to submit escalation decision to Intaris.",
        )
    request.app.state.pause_waiter.resolve(
        pause_id,
        PauseResolution(
            decision=payload.decision,
            data={"note": payload.note or ""},
        ),
    )
    return {"ok": True, "call_id": call_id, "decision": payload.decision}
