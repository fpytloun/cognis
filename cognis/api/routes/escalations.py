"""Escalation proxy routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cognis.api.common import require_current_user
from cognis.api.models import EscalationResolveRequest, EscalationResponse
from cognis.api.serializers import escalation_to_response

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
    require_current_user(request)
    await request.app.state.providers.guardrails.submit_decision(
        call_id,
        payload.decision,
        payload.note,
    )
    return {"ok": True, "call_id": call_id, "decision": payload.decision}
