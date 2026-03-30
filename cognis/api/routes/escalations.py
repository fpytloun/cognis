"""Escalation proxy routes."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Request

from cognis.api.common import require_current_user
from cognis.api.models import EscalationResolveRequest, EscalationResponse
from cognis.api.serializers import escalation_to_response
from cognis.core.agent_loop import PauseResolution

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

    # Resolve the PauseWaiter first (unblocks the agent loop)
    pause_id = f"escalation:{call_id}"
    request.app.state.pause_waiter.resolve(
        pause_id,
        PauseResolution(
            decision=payload.decision,
            data={"note": payload.note or ""},
        ),
    )

    # Submit to Intaris for audit trail
    with contextlib.suppress(Exception):
        await request.app.state.providers.guardrails.submit_decision(
            call_id,
            payload.decision,
            payload.note,
        )
    return {"ok": True, "call_id": call_id, "decision": payload.decision}
